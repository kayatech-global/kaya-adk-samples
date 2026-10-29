"""authn, authz and credentials ports (artifact 17.6) with development and standards-based providers."""

from __future__ import annotations

import fnmatch
import hmac
import secrets as _secrets
import time
from abc import abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..datatypes import ANONYMOUS, Credential, Decision, Principal
from ..errors import AuthenticationError, ConfigError
from ..trace.context import traced_client
from .base import HealthStatus, Provider, ProviderSettings
from .registry import register
from .secret import SecretNotFound


class TokenVault:
    """Process-local map handle -> bearer token, so tokens never enter session state or traces."""

    _tokens: dict[str, tuple[str, float]] = {}

    @classmethod
    def put(cls, token: str, ttl: float = 3600) -> str:
        """Store ``token`` for ``ttl`` seconds and return an opaque handle.

        The handle, not the token, is what travels in ``Principal.token_ref``, so tokens stay out of session state and traces.
        """
        handle = "tok_" + _secrets.token_urlsafe(12)
        cls._tokens[handle] = (token, time.time() + ttl)
        cls._gc()
        return handle

    @classmethod
    def get(cls, handle: str | None) -> str | None:
        """Return the token for ``handle``, or None when the handle is empty, unknown or expired."""
        if not handle:
            return None
        item = cls._tokens.get(handle)
        return item[0] if item and item[1] > time.time() else None

    @classmethod
    def _gc(cls) -> None:
        now = time.time()
        for k in [k for k, v in cls._tokens.items() if v[1] <= now]:
            cls._tokens.pop(k, None)


def bearer_token(request: Mapping[str, Any]) -> str | None:
    """Return the token from the request's ``Authorization: Bearer`` header (header name case-insensitive), or None."""
    headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
    value = headers.get("authorization", "")
    return value.split(" ", 1)[1].strip() if value.lower().startswith("bearer ") else None


# ---------------------------------------------------------------- authn


class Authenticator(Provider):
    """Port of the ``authn`` kind: turns the headers of a request into a Principal."""
    kind = "authn"

    @abstractmethod
    async def authenticate(self, request: Mapping[str, Any]) -> Principal:
        """request: {"headers": {...}}. Raise AuthenticationError when the caller cannot be identified."""

    def a2a_security(self) -> dict[str, dict[str, Any]]:
        """How a caller authenticates, as A2A ``securitySchemes`` for the agent card. The default is a bearer token.

        Return ``{}`` when callers present nothing (the card then lists no requirement).
        """
        return {"bearer": {"type": "http", "scheme": "bearer"}}


@register
class AnonymousAuthn(Authenticator):
    """Development only: everyone is 'anonymous'. Refused by the production profile."""

    name = "anonymous"

    async def health(self) -> HealthStatus:
        """Report unhealthy under the production profile, healthy otherwise."""
        if self.ctx.profile == "production":
            return HealthStatus(False, "anonymous authn is not allowed with profile 'production'")
        return HealthStatus(True)

    async def authenticate(self, request: Mapping[str, Any]) -> Principal:
        """Return the shared anonymous principal without inspecting the request; refuse every caller under production."""
        if self.ctx.profile == "production":
            raise AuthenticationError("anonymous authn is not allowed with profile 'production'")
        return ANONYMOUS

    def a2a_security(self) -> dict[str, dict[str, Any]]:
        """Nothing to present."""
        return {}


@register
class MockAuthn(Authenticator):
    """Test authn: a bearer token listed in ``tokens`` maps to a fixed principal."""
    name = "mock"

    class Settings(ProviderSettings):
        tokens: dict[str, dict[str, Any]] = {}  # bearer token -> {subject, roles, tenant}

    async def authenticate(self, request: Mapping[str, Any]) -> Principal:
        """Look the bearer token up in ``tokens``; raise AuthenticationError when it is missing or unknown."""
        token = bearer_token(request)
        info = self.settings.tokens.get(token or "")
        if info is None:
            raise AuthenticationError("invalid or missing bearer token")
        return Principal(
            info["subject"], info.get("tenant"), tuple(info.get("roles", ())), info.get("claims", {}), TokenVault.put(token or "")
        )


@register
class TrustedHeaderAuthn(Authenticator):
    """Behind a gateway that already authenticated the caller and forwards identity in headers."""

    name = "trusted_header"

    class Settings(ProviderSettings):
        subject_header: str = "x-user"
        roles_header: str = "x-roles"
        tenant_header: str = "x-tenant"

    async def authenticate(self, request: Mapping[str, Any]) -> Principal:
        """Build the principal from the configured subject, roles and tenant headers.

        Roles are comma-separated. Raises AuthenticationError when the subject header is absent.
        """
        headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
        subject = headers.get(self.settings.subject_header.lower())
        if not subject:
            raise AuthenticationError(f"missing header '{self.settings.subject_header}'")
        roles = tuple(r.strip() for r in headers.get(self.settings.roles_header.lower(), "").split(",") if r.strip())
        return Principal(subject, headers.get(self.settings.tenant_header.lower()), roles)

    def a2a_security(self) -> dict[str, dict[str, Any]]:
        """The gateway in front authenticates the caller; the agent itself asks for nothing."""
        return {}


@register
class ApiKeyAuthn(Authenticator):
    """Authenticate by an API key header, matched against inline ``keys`` or the secrets named in ``key_secrets``."""
    name = "api_key"

    class Settings(ProviderSettings):
        header: str = "x-api-key"
        keys: dict[str, dict[str, Any]] = {}  # key -> {subject, roles}; prefer key_secrets in production
        key_secrets: dict[str, str] = {}  # secret name -> subject

    async def authenticate(self, request: Mapping[str, Any]) -> Principal:
        """Return the principal for the presented key; raise AuthenticationError when the key is missing or unknown."""
        headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
        key = headers.get(self.settings.header.lower(), "")
        info = next((v for k, v in self.settings.keys.items() if key and hmac.compare_digest(k, key)), None)
        if info is None and key:
            for secret_name, subject in self.settings.key_secrets.items():
                try:
                    expected = await self.ctx.provider("secret").get(secret_name)
                except SecretNotFound:
                    continue  # one unset key must not lock out the callers of the others
                if hmac.compare_digest(expected, key):
                    info = {"subject": subject}
                    break
        if info is None:
            raise AuthenticationError("invalid or missing API key")
        return Principal(info["subject"], info.get("tenant"), tuple(info.get("roles", ())))

    def a2a_security(self) -> dict[str, dict[str, Any]]:
        """An API key in the configured header."""
        return {"apiKey": {"type": "apiKey", "in": "header", "name": self.settings.header}}


class _JwtBase(Authenticator):
    """Shared claim mapping for JWT verifiers."""

    def a2a_security(self) -> dict[str, dict[str, Any]]:
        """A signed JWT as a bearer token."""
        return {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}

    def _principal(self, claims: dict[str, Any], token: str, roles_claim: str, subject_claim: str) -> Principal:
        roles: list[str] = []
        node: Any = claims
        for part in roles_claim.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, list):
            roles = [str(r) for r in node]
        subject = claims.get(subject_claim) or claims.get("sub") or "unknown"
        keep: dict[str, Any] = {k: claims[k] for k in ("email", "preferred_username", "given_name", "family_name", "sid") if k in claims}
        return Principal(str(subject), claims.get("tenant"), tuple(roles), keep, TokenVault.put(token))


@register
class KeycloakStaticKeyAuthn(_JwtBase):
    """RS256 against one static realm public key, no iss/aud check."""

    name = "keycloak_static_key"

    class Settings(ProviderSettings):
        public_key: str = ""
        public_key_secret: str = ""
        subject_claim: str = "email"
        roles_claim: str = "realm_access.roles"
        verify_iat: bool = False

    async def _key(self) -> str:
        raw = self.settings.public_key or (
            await self.ctx.provider("secret").get(self.settings.public_key_secret) if self.settings.public_key_secret else ""
        )
        if not raw:
            raise ConfigError("authn 'keycloak_static_key' needs public_key or public_key_secret")
        raw = raw.strip()
        if "BEGIN" not in raw:
            raw = f"-----BEGIN PUBLIC KEY-----\n{raw}\n-----END PUBLIC KEY-----"
        return raw

    async def health(self) -> HealthStatus:
        """Healthy when a public key can be resolved from settings or the secret factory."""
        try:
            await self._key()
            return HealthStatus(True)
        except Exception as exc:
            return HealthStatus(False, str(exc))

    async def authenticate(self, request: Mapping[str, Any]) -> Principal:
        """Verify the RS256 token and map its claims to a principal.

        Raises AuthenticationError when the token is missing or rejected. Only the exception type is reported, so token content does not leak into errors.
        """
        import jwt

        token = bearer_token(request)
        if not token:
            raise AuthenticationError("missing bearer token")
        try:
            claims = jwt.decode(
                token,
                await self._key(),
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_iss": False, "verify_iat": self.settings.verify_iat, "require": ["exp"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"token rejected: {type(exc).__name__}") from exc
        return self._principal(claims, token, self.settings.roles_claim, self.settings.subject_claim)


@register
class JwtOidcAuthn(_JwtBase):
    """Generic OIDC verifier: JWKS (or a static key), issuer and audience checks."""

    name = "jwt_oidc"

    class Settings(ProviderSettings):
        jwks_url: str = ""
        issuer: str = ""
        audience: str = ""
        algorithms: list[str] = ["RS256"]
        subject_claim: str = "sub"
        roles_claim: str = "roles"
        leeway: int = 30

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self._jwks: Any = None

    async def health(self) -> HealthStatus:
        """Healthy only when both ``jwks_url`` and ``issuer`` are set. Makes no network call."""
        ok = bool(self.settings.jwks_url and self.settings.issuer)
        return HealthStatus(ok, "" if ok else "jwt_oidc needs jwks_url and issuer")

    async def authenticate(self, request: Mapping[str, Any]) -> Principal:
        """Verify the token against the JWKS with issuer and audience checks and map its claims to a principal.

        Raises AuthenticationError when the token is missing or rejected. The key lookup runs in a worker thread because the PyJWT client blocks.
        """
        import jwt

        token = bearer_token(request)
        if not token:
            raise AuthenticationError("missing bearer token")
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(self.settings.jwks_url, cache_keys=True)
        try:
            import asyncio

            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=self.settings.algorithms,
                issuer=self.settings.issuer or None,
                audience=self.settings.audience or None,
                leeway=self.settings.leeway,
                options={"verify_aud": bool(self.settings.audience), "require": ["exp"]},  # a token without expiry never ends
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"token rejected: {type(exc).__name__}") from exc
        return self._principal(claims, token, self.settings.roles_claim, self.settings.subject_claim)


# ---------------------------------------------------------------- authz


class Authorizer(Provider):
    """Port of the ``authz`` kind: decides whether a principal may perform an action on a resource."""
    kind = "authz"

    @abstractmethod
    async def check(self, principal: Principal, action: str, resource: Mapping[str, Any], context: Mapping[str, Any]) -> Decision:
        """Decide whether ``principal`` may perform ``action`` on ``resource``.

        Returns a Decision with effect allow, deny or require_approval. ``context`` carries run-level facts the policy may use.
        """


@register
class AllowAllAuthz(Authorizer):
    """Permit every action. Offers no protection; use it only where authorization is enforced elsewhere."""
    name = "allow_all"

    async def check(self, principal, action, resource, context):
        """Always return an allow decision."""
        return Decision("allow", "allow_all")


@register
class RulesAuthz(Authorizer):
    """YAML rules: first match wins. {effect, roles, subjects, actions: [glob], resources: [glob over resource id]}"""

    name = "rules"

    class Settings(ProviderSettings):
        file: str = ""
        rules: list[dict[str, Any]] = []
        default: str = "deny"

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        rules = list(self.settings.rules)
        if self.settings.file:
            path = Path(self.settings.file)
            path = path if path.is_absolute() else self.ctx.export_root / path
            data = yaml.safe_load(path.read_text()) or {}
            rules = list(data.get("rules", [])) + rules
        effects = {"allow", "deny", "require_approval"}
        typos = [str(r.get("effect")) for r in rules if r.get("effect", "allow") not in effects]
        if self.settings.default not in effects or typos:
            raise ConfigError(f"rules authz: effects must be one of {sorted(effects)}; got default '{self.settings.default}', rules {typos}")
        self.rules = rules

    async def check(self, principal, action, resource, context):
        """Return the decision of the first rule matching roles, subjects, actions and resources, else the ``default`` effect."""
        rid = str(resource.get("id", resource.get("name", "")))
        for rule in self.rules:
            if rule.get("roles") and not set(rule["roles"]) & set(principal.roles):
                continue
            if rule.get("subjects") and principal.subject not in rule["subjects"]:
                continue
            if rule.get("actions") and not any(fnmatch.fnmatchcase(action, p) for p in rule["actions"]):
                continue
            if rule.get("resources") and not any(fnmatch.fnmatchcase(rid, p) for p in rule["resources"]):
                continue
            return Decision(rule.get("effect", "allow"), rule.get("reason", "rule matched"), approvers=rule.get("approvers"))
        return Decision(self.settings.default, "no rule matched")


@register
class HttpPdpAuthz(Authorizer):
    """Asks a policy decision service: POST {principal, action, resource, context} -> {effect|allow, reason}."""

    name = "http_pdp"

    class Settings(ProviderSettings):
        url: str
        timeout: float = 5
        headers: dict[str, str] = {}
        token_secret: str = ""

    transport: httpx.AsyncBaseTransport | None = None

    async def check(self, principal, action, resource, context):
        """POST the request to the policy decision service and map its answer to a Decision.

        Raises httpx.HTTPStatusError when the service replies with an error status.
        """
        headers = dict(self.settings.headers)
        if self.settings.token_secret:
            headers["Authorization"] = "Bearer " + await self.ctx.provider("secret").get(self.settings.token_secret)
        async with traced_client(timeout=self.settings.timeout, transport=self.transport) as client:
            resp = await client.post(
                self.settings.url,
                headers=headers,
                json={"principal": principal.public(), "action": action, "resource": dict(resource), "context": dict(context)},
            )
        resp.raise_for_status()
        body = resp.json()
        effect = body.get("effect") or ("allow" if body.get("allow") is True else "deny")  # "allow": "false" is not an allow
        return Decision(effect, str(body.get("reason", "")), tuple(body.get("obligations", ())), body.get("approvers"))


@register
class MockAuthz(Authorizer):
    """Test authz: denies or requires approval for actions matching the configured globs, allows the rest, and records every check."""
    name = "mock"

    class Settings(ProviderSettings):
        deny_actions: list[str] = []
        approval_actions: list[str] = []
        approvers: dict[str, Any] = {}

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self.checks: list[tuple[str, str]] = []

    async def check(self, principal, action, resource, context):
        """Record the check and return deny, require_approval or allow according to the configured action globs."""
        rid = str(resource.get("id", ""))
        self.checks.append((action, rid))
        key = f"{action}:{rid}"
        if any(fnmatch.fnmatchcase(key, p) or fnmatch.fnmatchcase(action, p) for p in self.settings.deny_actions):
            return Decision("deny", "mock deny")
        if any(fnmatch.fnmatchcase(key, p) or fnmatch.fnmatchcase(action, p) for p in self.settings.approval_actions):
            return Decision("require_approval", "mock approval", approvers=self.settings.approvers or None)
        return Decision("allow", "mock allow")


# ---------------------------------------------------------------- credentials


class CredentialProvider(Provider):
    """Port of the ``credentials`` kind: produces outbound credentials for a target on behalf of a principal."""
    kind = "credentials"

    @abstractmethod
    async def get(self, target: str, principal: Principal, scope: str | None = None) -> Credential:
        """Return the Credential to attach to a call to ``target``, or an empty Credential when none applies.

        Args:
            target: Identifier of the callee. Most providers match it against glob patterns.
            principal: The caller on whose behalf the call is made.
            scope: Optional scope to request, for providers that support one.
        """


@register
class NoCredentials(CredentialProvider):
    """Never supplies credentials."""
    name = "none"

    async def get(self, target, principal, scope=None):
        """Return an empty Credential."""
        return Credential()


@register
class PassthroughCredentials(CredentialProvider):
    """Forward the caller's own token to the target. Off unless chosen as the credentials provider; limit with ``targets``."""

    name = "passthrough"

    class Settings(ProviderSettings):
        targets: list[str] = ["*"]
        header: str = "Authorization"

    async def get(self, target, principal, scope=None):
        """Return the caller's own bearer token as a header when ``target`` matches ``targets``, else an empty Credential."""
        if not any(fnmatch.fnmatchcase(target, p) for p in self.settings.targets):
            return Credential()
        token = TokenVault.get(principal.token_ref)
        return Credential({self.settings.header: f"Bearer {token}"}) if token else Credential()


@register
class StaticSecretCredentials(CredentialProvider):
    """target glob -> {header: secret name}. Secrets are fetched per call through the secret factory."""

    name = "static_secret"

    class Settings(ProviderSettings):
        mappings: dict[str, dict[str, str]] = {}

    async def get(self, target, principal, scope=None):
        """Return headers whose values are secrets, using the first ``mappings`` glob that matches ``target``."""
        for pattern, headers in self.settings.mappings.items():
            if fnmatch.fnmatchcase(target, pattern):
                secret = self.ctx.provider("secret")
                return Credential({h: await secret.get(name) for h, name in headers.items()})
        return Credential()


@register
class OAuthClientCredentials(CredentialProvider):
    """OAuth2 client-credentials grant against ``token_url``, with the token cached until shortly before it expires."""
    name = "oauth_client_credentials"

    class Settings(ProviderSettings):
        token_url: str
        client_id: str
        client_secret_name: str
        scope: str = ""
        audience: str = ""
        targets: list[str] = ["*"]

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self._cache: tuple[str, float] | None = None

    async def get(self, target, principal, scope=None):
        """Return a service bearer token for a matching target, fetching a new one when the cached token expires within 30 seconds.

        Raises httpx.HTTPStatusError when the token endpoint refuses.
        """
        if not any(fnmatch.fnmatchcase(target, p) for p in self.settings.targets):
            return Credential()
        if self._cache and self._cache[1] > time.time() + 30:
            return Credential({"Authorization": f"Bearer {self._cache[0]}"}, self._cache[1])
        data = {
            "grant_type": "client_credentials",
            "client_id": self.settings.client_id,
            "client_secret": await self.ctx.provider("secret").get(self.settings.client_secret_name),
        }
        for k in ("scope", "audience"):
            if getattr(self.settings, k):
                data[k] = getattr(self.settings, k)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.settings.token_url, data=data)
        resp.raise_for_status()
        body = resp.json()
        self._cache = (body["access_token"], time.time() + float(body.get("expires_in", 300)))
        return Credential({"Authorization": f"Bearer {self._cache[0]}"}, self._cache[1])


@register
class TokenExchangeCredentials(CredentialProvider):
    """RFC 8693: exchange the caller's token for one valid at the target (on behalf of the caller)."""

    name = "token_exchange"

    class Settings(ProviderSettings):
        token_url: str
        client_id: str
        client_secret_name: str = ""
        audience_map: dict[str, str] = {}  # target glob -> audience
        targets: list[str] = ["*"]

    transport: httpx.AsyncBaseTransport | None = None

    async def get(self, target, principal, scope=None):
        """Exchange the caller's stored token for one valid at ``target``.

        Returns an empty Credential when the target does not match or the caller has no stored token. The exchange runs on every call; the result is not cached.
        """
        if not any(fnmatch.fnmatchcase(target, p) for p in self.settings.targets):
            return Credential()
        subject_token = TokenVault.get(principal.token_ref)
        if not subject_token:
            return Credential()
        audience = next((a for pat, a in self.settings.audience_map.items() if fnmatch.fnmatchcase(target, pat)), "")
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "client_id": self.settings.client_id,
        }
        if audience:
            data["audience"] = audience
        if scope:
            data["scope"] = scope
        if self.settings.client_secret_name:
            data["client_secret"] = await self.ctx.provider("secret").get(self.settings.client_secret_name)
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            resp = await client.post(self.settings.token_url, data=data)
        resp.raise_for_status()
        body = resp.json()
        return Credential({"Authorization": f"Bearer {body['access_token']}"}, time.time() + float(body.get("expires_in", 300)))
