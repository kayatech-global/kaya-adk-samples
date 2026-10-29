"""The built-in authn, authz and credentials providers, offline: in-process HTTP, locally generated keys, a fake clock."""

import time
from urllib.parse import parse_qsl

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from retail_kyc_onboarding._engine.datatypes import ANONYMOUS, Decision, Principal
from retail_kyc_onboarding._engine.engine.guard import Guard
from retail_kyc_onboarding._engine.errors import AuthenticationError, ConfigError
from retail_kyc_onboarding._engine.factories import security
from retail_kyc_onboarding._engine.factories.base import BuildContext
from retail_kyc_onboarding._engine.factories.registry import REGISTRY
from retail_kyc_onboarding._engine.factories.secret import MockSecrets, SecretNotFound
from retail_kyc_onboarding._engine.factories.security import (
    AllowAllAuthz,
    AnonymousAuthn,
    ApiKeyAuthn,
    HttpPdpAuthz,
    JwtOidcAuthn,
    KeycloakStaticKeyAuthn,
    MockAuthn,
    MockAuthz,
    NoCredentials,
    OAuthClientCredentials,
    PassthroughCredentials,
    RulesAuthz,
    StaticSecretCredentials,
    TokenExchangeCredentials,
    TokenVault,
    TrustedHeaderAuthn,
    bearer_token,
)
from retail_kyc_onboarding._engine.trace import Tracer

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
ISSUER, AUDIENCE, JWKS_URL = "https://idp.test/realms/kyc", "kyc-agent", "https://idp.test/realms/kyc/certs"
JWKS = {"keys": [{**jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k1", "use": "sig", "alg": "RS256"}]}
ANA = Principal("ana", "acme", ("officer",))


def ctx(**secrets: str) -> BuildContext:
    """A build context whose secret factory serves ``secrets``."""
    return BuildContext(env={}, built={"secret": MockSecrets({"values": secrets})})


def bearer(token: str) -> dict:
    """A request carrying ``token`` as its bearer token."""
    return {"headers": {"Authorization": f"Bearer {token}"}}


def signed(claims: dict, key=KEY, kid: str = "k1", algorithm: str = "RS256", expiring: bool = True) -> str:
    """A JWT of ``claims`` signed with ``key``; it expires in five minutes unless ``claims`` say otherwise or ``expiring`` is off."""
    claims = {"exp": int(time.time()) + 300, **claims} if expiring else claims
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def oidc_claims(**extra) -> dict:
    """Claims a JwtOidcAuthn with the test issuer and audience accepts."""
    now = int(time.time())
    return {"sub": "ana", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300, "roles": ["officer"], **extra}


class Clock:
    """Stands in for the ``time`` module of the security providers, so expiry can be stepped over without sleeping."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


class CountingSecrets:
    """A secret factory that records every name it is asked for."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values, self.asked = values, []

    async def get(self, name: str) -> str:
        self.asked.append(name)
        return self.values[name]


class Endpoint:
    """An in-process HTTP endpoint that answers with ``reply`` and records each request's form or JSON body and headers."""

    def __init__(self, reply=None, status: int = 200, error: Exception | None = None) -> None:
        self.reply, self.status, self.error, self.requests = reply, status, error, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.error:
            raise self.error
        self.requests.append(request)
        return httpx.Response(self.status, json=self.reply)

    def form(self, i: int = -1) -> dict:
        return dict(parse_qsl(self.requests[i].content.decode()))


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(security, "time", fake)
    return fake


@pytest.fixture
def jwks(monkeypatch) -> list:
    """Serve ``JWKS`` to every PyJWKClient in process; the returned list grows by one per fetch."""
    fetches: list = []

    def fetch(client):
        fetches.append(client.uri)
        return JWKS

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch)
    return fetches


def oauth_endpoint(monkeypatch, endpoint: Endpoint) -> Endpoint:
    """Route the plain httpx clients OAuthClientCredentials opens to ``endpoint``."""
    real = httpx.AsyncClient
    monkeypatch.setattr(security.httpx, "AsyncClient", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(endpoint)}))
    return endpoint


def test_every_builtin_security_provider_is_registered_under_its_name():
    assert {"anonymous", "mock", "trusted_header", "api_key", "keycloak_static_key", "jwt_oidc"} <= set(REGISTRY.names("authn"))
    assert {"allow_all", "rules", "http_pdp", "mock"} <= set(REGISTRY.names("authz"))
    expected = {"none", "passthrough", "static_secret", "oauth_client_credentials", "token_exchange"}
    assert expected <= set(REGISTRY.names("credentials"))


# ---------------------------------------------------------------- token vault, bearer token


def test_a_stored_token_comes_back_by_its_handle_and_the_handle_is_not_the_token():
    handle = TokenVault.put("secret-token", ttl=60)
    assert handle.startswith("tok_") and "secret-token" not in handle
    assert TokenVault.get(handle) == "secret-token"
    assert TokenVault.put("secret-token") != handle  # every put is a fresh handle


@pytest.mark.parametrize("handle", [None, "", "tok_unknown"])
def test_an_empty_or_unknown_handle_has_no_token(handle):
    assert TokenVault.get(handle) is None


def test_a_token_expires_after_its_ttl_and_is_dropped_on_the_next_put(clock):
    handle = TokenVault.put("short-lived", ttl=10)
    clock.now += 11
    assert TokenVault.get(handle) is None
    TokenVault.put("another", ttl=10)
    assert handle not in TokenVault._tokens


@pytest.mark.parametrize(
    ("headers", "token"),
    [
        ({"Authorization": "Bearer abc"}, "abc"),
        ({"authorization": "bearer  abc "}, "abc"),
        ({"AUTHORIZATION": "BEARER abc"}, "abc"),
        ({"Authorization": "Basic YWJj"}, None),
        ({"Authorization": "Bearer"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_the_bearer_token_is_read_from_the_authorization_header_only(headers, token):
    assert bearer_token({"headers": headers}) == token


# ---------------------------------------------------------------- anonymous, mock, trusted header


async def test_anonymous_authn_lets_everyone_in_but_refuses_and_is_unhealthy_in_production():
    assert await AnonymousAuthn().authenticate({}) is ANONYMOUS
    assert (await AnonymousAuthn(ctx=BuildContext(profile="development")).health()).ok
    production = AnonymousAuthn(ctx=BuildContext(profile="production"))
    status = await production.health()
    assert not status.ok and "production" in status.detail
    with pytest.raises(AuthenticationError, match="production"):
        await production.authenticate({})


async def test_mock_authn_maps_a_listed_token_to_its_principal_and_rejects_the_rest():
    authn = MockAuthn({"tokens": {"t1": {"subject": "ana", "tenant": "acme", "roles": ["officer"], "claims": {"dept": "kyc"}}}})
    principal = await authn.authenticate(bearer("t1"))
    assert (principal.subject, principal.tenant, principal.roles, principal.claims) == ("ana", "acme", ("officer",), {"dept": "kyc"})
    assert TokenVault.get(principal.token_ref) == "t1"
    for request in (bearer("t2"), {"headers": {}}):
        with pytest.raises(AuthenticationError, match="invalid or missing bearer token"):
            await authn.authenticate(request)


async def test_trusted_headers_become_the_principal_whatever_their_case():
    headers = {"X-User": "ana", "X-Roles": " officer, ,reviewer ", "X-Tenant": "acme"}
    principal = await TrustedHeaderAuthn().authenticate({"headers": headers})
    assert (principal.subject, principal.tenant, principal.roles) == ("ana", "acme", ("officer", "reviewer"))
    assert principal.token_ref is None


async def test_trusted_header_names_are_configurable_and_roles_and_tenant_are_optional():
    authn = TrustedHeaderAuthn({"subject_header": "X-Forwarded-Email", "roles_header": "X-Groups", "tenant_header": "X-Org"})
    principal = await authn.authenticate({"headers": {"x-forwarded-email": "ana@bank.test"}})
    assert (principal.subject, principal.tenant, principal.roles) == ("ana@bank.test", None, ())


@pytest.mark.parametrize("headers", [{}, {"x-user": ""}, {"x-roles": "admin"}])
async def test_a_request_without_the_subject_header_is_not_authenticated(headers):
    with pytest.raises(AuthenticationError, match="missing header 'x-user'"):
        await TrustedHeaderAuthn().authenticate({"headers": headers})


# ---------------------------------------------------------------- api key


async def test_an_inline_api_key_maps_to_its_principal():
    authn = ApiKeyAuthn({"keys": {"k-123": {"subject": "partner", "tenant": "acme", "roles": ["caller"]}}})
    principal = await authn.authenticate({"headers": {"X-API-Key": "k-123"}})
    assert (principal.subject, principal.tenant, principal.roles) == ("partner", "acme", ("caller",))


async def test_an_api_key_held_as_a_secret_is_matched_by_its_logical_name():
    authn = ApiKeyAuthn({"key_secrets": {"bank_key": "bank", "partner_key": "partner"}}, ctx(bank_key="b-1", partner_key="p-1"))
    assert (await authn.authenticate({"headers": {"x-api-key": "p-1"}})).subject == "partner"
    assert (await authn.authenticate({"headers": {"x-api-key": "b-1"}})).subject == "bank"


async def test_the_api_key_header_is_configurable():
    authn = ApiKeyAuthn({"header": "X-Partner-Key", "keys": {"k": {"subject": "partner"}}})
    assert (await authn.authenticate({"headers": {"x-partner-key": "k"}})).subject == "partner"
    with pytest.raises(AuthenticationError):
        await authn.authenticate({"headers": {"x-api-key": "k"}})


async def test_a_wrong_api_key_is_rejected_after_every_secret_is_compared():
    secrets = CountingSecrets({"bank_key": "b-1", "partner_key": "p-1"})
    authn = ApiKeyAuthn({"key_secrets": {"bank_key": "bank", "partner_key": "partner"}}, BuildContext(built={"secret": secrets}))
    with pytest.raises(AuthenticationError, match="invalid or missing API key"):
        await authn.authenticate({"headers": {"x-api-key": "guess"}})
    assert secrets.asked == ["bank_key", "partner_key"]


async def test_a_missing_api_key_is_rejected_without_reading_any_secret():
    secrets = CountingSecrets({"bank_key": "b-1"})
    authn = ApiKeyAuthn({"key_secrets": {"bank_key": "bank"}}, BuildContext(built={"secret": secrets}))
    with pytest.raises(AuthenticationError, match="invalid or missing API key"):
        await authn.authenticate({"headers": {}})
    assert secrets.asked == []


# ---------------------------------------------------------------- keycloak static key


async def test_a_keycloak_token_maps_to_a_principal_with_the_configured_subject_and_roles():
    claims = {
        "sub": "uuid-1",
        "email": "ana@bank.test",
        "preferred_username": "ana",
        "realm_access": {"roles": ["officer", "reviewer"]},
        "tenant": "acme",
        "internal": "not kept",
        "exp": int(time.time()) + 300,
    }
    token = signed(claims)
    principal = await KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM}).authenticate(bearer(token))
    assert (principal.subject, principal.tenant, principal.roles) == ("ana@bank.test", "acme", ("officer", "reviewer"))
    assert principal.claims == {"email": "ana@bank.test", "preferred_username": "ana"}
    assert TokenVault.get(principal.token_ref) == token and token not in str(principal.to_state())


async def test_a_keycloak_key_may_be_a_bare_base64_body_held_as_a_secret():
    body = "".join(line for line in PUBLIC_PEM.strip().splitlines() if "-----" not in line)
    authn = KeycloakStaticKeyAuthn({"public_key_secret": "realm_key"}, ctx(realm_key=f"  {body}\n"))
    assert (await authn.health()).ok
    assert (await authn.authenticate(bearer(signed({"email": "ana@bank.test"})))).subject == "ana@bank.test"


@pytest.mark.parametrize(
    ("claims", "subject_claim", "subject"),
    [({"sub": "uuid-1"}, "email", "uuid-1"), ({}, "email", "unknown"), ({"upn": "ana", "sub": "uuid-1"}, "upn", "ana")],
)
async def test_the_subject_falls_back_to_sub_then_unknown(claims, subject_claim, subject):
    authn = KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM, "subject_claim": subject_claim})
    assert (await authn.authenticate(bearer(signed(claims)))).subject == subject


@pytest.mark.parametrize(
    ("roles_claim", "claims", "roles"),
    [
        ("resource_access.kyc.roles", {"resource_access": {"kyc": {"roles": ["approver"]}}}, ("approver",)),
        ("realm_access.roles", {"realm_access": {"roles": "officer"}}, ()),  # not a list: no roles
        ("realm_access.roles", {"realm_access": ["officer"]}, ()),
        ("realm_access.roles", {}, ()),
    ],
)
async def test_roles_are_read_from_a_dotted_claim_path_and_only_a_list_counts(roles_claim, claims, roles):
    authn = KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM, "roles_claim": roles_claim})
    assert (await authn.authenticate(bearer(signed({"sub": "ana", **claims})))).roles == roles


@pytest.mark.parametrize(
    ("token", "error"),
    [
        (signed({"sub": "ana", "exp": int(time.time()) - 60}), "ExpiredSignatureError"),
        (signed({"sub": "ana"}, key=OTHER_KEY), "InvalidSignatureError"),
        (signed({"sub": "ana"}, key="shared-secret-long-enough-for-hs256-x", algorithm="HS256"), "InvalidAlgorithmError"),
        ("not.a.jwt", "DecodeError"),
    ],
)
async def test_a_keycloak_token_that_fails_verification_is_rejected_without_echoing_it(token, error):
    with pytest.raises(AuthenticationError, match=f"token rejected: {error}") as err:
        await KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM}).authenticate(bearer(token))
    assert token not in str(err.value)


async def test_the_static_key_verifier_ignores_issuer_and_audience():
    token = signed({"sub": "ana", "iss": "https://elsewhere.test", "aud": "another-client"})
    assert (await KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM}).authenticate(bearer(token))).subject == "ana"


async def test_a_token_issued_in_the_future_is_rejected_only_when_verify_iat_is_on():
    token = signed({"sub": "ana", "iat": int(time.time()) + 3600})
    assert (await KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM}).authenticate(bearer(token))).subject == "ana"
    with pytest.raises(AuthenticationError, match="ImmatureSignatureError"):
        await KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM, "verify_iat": True}).authenticate(bearer(token))


async def test_a_keycloak_request_without_a_bearer_token_is_rejected():
    with pytest.raises(AuthenticationError, match="missing bearer token"):
        await KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM}).authenticate({"headers": {"Authorization": "Basic YWJj"}})


async def test_a_keycloak_verifier_without_a_key_is_unhealthy_and_a_config_error():
    authn = KeycloakStaticKeyAuthn({}, ctx())
    status = await authn.health()
    assert not status.ok and "needs public_key or public_key_secret" in status.detail
    with pytest.raises(ConfigError, match="needs public_key"):
        await authn.authenticate(bearer(signed({"sub": "ana"})))


async def test_a_keycloak_key_secret_that_is_missing_makes_the_verifier_unhealthy():
    status = await KeycloakStaticKeyAuthn({"public_key_secret": "realm_key"}, ctx()).health()
    assert not status.ok and "'realm_key'" in status.detail


# ---------------------------------------------------------------- jwt oidc


def oidc(**settings) -> JwtOidcAuthn:
    """A JwtOidcAuthn for the test issuer, audience and JWKS URL."""
    return JwtOidcAuthn({"jwks_url": JWKS_URL, "issuer": ISSUER, "audience": AUDIENCE, **settings})


async def test_an_oidc_token_maps_to_a_principal_with_the_configured_subject_and_roles(jwks):
    token = signed(oidc_claims(email="ana@bank.test", sid="s-1", tenant="acme"))
    principal = await oidc().authenticate(bearer(token))
    assert (principal.subject, principal.tenant, principal.roles) == ("ana", "acme", ("officer",))
    assert principal.claims == {"email": "ana@bank.test", "sid": "s-1"}
    assert TokenVault.get(principal.token_ref) == token
    assert jwks == [JWKS_URL]


async def test_custom_subject_and_roles_claims_are_honoured(jwks):
    token = signed(oidc_claims(email="ana@bank.test", groups={"kyc": ["approver", 7]}))
    principal = await oidc(subject_claim="email", roles_claim="groups.kyc").authenticate(bearer(token))
    assert (principal.subject, principal.roles) == ("ana@bank.test", ("approver", "7"))


async def test_the_jwks_is_fetched_once_then_its_keys_are_cached(jwks):
    authn = oidc()
    for _ in range(3):
        await authn.authenticate(bearer(signed(oidc_claims())))
    assert len(jwks) == 1


@pytest.mark.parametrize(
    ("claims", "key", "kid", "error"),
    [
        (oidc_claims(exp=int(time.time()) - 120), KEY, "k1", "ExpiredSignatureError"),
        (oidc_claims(aud="another-client"), KEY, "k1", "InvalidAudienceError"),
        (oidc_claims(iss="https://evil.test"), KEY, "k1", "InvalidIssuerError"),
        (oidc_claims(), OTHER_KEY, "k1", "InvalidSignatureError"),
        (oidc_claims(), OTHER_KEY, "k-unknown", "PyJWKClientError"),
    ],
)
async def test_an_oidc_token_that_fails_verification_is_rejected(jwks, claims, key, kid, error):
    token = signed(claims, key=key, kid=kid)
    with pytest.raises(AuthenticationError, match=f"token rejected: {error}") as err:
        await oidc().authenticate(bearer(token))
    assert token not in str(err.value)


async def test_a_token_expired_within_the_leeway_is_still_accepted(jwks):
    token = signed(oidc_claims(exp=int(time.time()) - 10))
    assert (await oidc(leeway=30).authenticate(bearer(token))).subject == "ana"
    with pytest.raises(AuthenticationError, match="ExpiredSignatureError"):
        await oidc(leeway=0).authenticate(bearer(token))


async def test_without_an_audience_setting_the_audience_is_not_checked(jwks):
    token = signed(oidc_claims(aud="anything"))
    assert (await oidc(audience="").authenticate(bearer(token))).subject == "ana"


async def test_an_algorithm_outside_the_allowed_list_is_rejected(jwks):
    with pytest.raises(AuthenticationError, match="token rejected"):
        await oidc(algorithms=["ES256"]).authenticate(bearer(signed(oidc_claims())))


async def test_an_unreachable_jwks_rejects_the_caller_instead_of_letting_them_in(monkeypatch):
    def down(client):
        raise jwt.PyJWKClientConnectionError("connection refused")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", down)
    with pytest.raises(AuthenticationError, match="token rejected: PyJWKClientConnectionError"):
        await oidc().authenticate(bearer(signed(oidc_claims())))


async def test_an_oidc_request_without_a_bearer_token_is_rejected_before_any_key_lookup(jwks):
    authn = oidc()
    with pytest.raises(AuthenticationError, match="missing bearer token"):
        await authn.authenticate({"headers": {}})
    assert jwks == [] and authn._jwks is None


@pytest.mark.parametrize(("jwks_url", "issuer", "ok"), [(JWKS_URL, ISSUER, True), ("", ISSUER, False), (JWKS_URL, "", False)])
async def test_oidc_health_needs_a_jwks_url_and_an_issuer_without_calling_out(jwks, jwks_url, issuer, ok):
    status = await JwtOidcAuthn({"jwks_url": jwks_url, "issuer": issuer}).health()
    assert status.ok is ok and (ok or "needs jwks_url and issuer" in status.detail)
    assert jwks == []


# ---------------------------------------------------------------- a2a security schemes


@pytest.mark.parametrize(
    ("provider", "schemes"),
    [
        (AnonymousAuthn(), {}),
        (TrustedHeaderAuthn(), {}),
        (MockAuthn(), {"bearer": {"type": "http", "scheme": "bearer"}}),
        (ApiKeyAuthn(), {"apiKey": {"type": "apiKey", "in": "header", "name": "x-api-key"}}),
        (ApiKeyAuthn({"header": "X-Partner-Key"}), {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Partner-Key"}}),
        (KeycloakStaticKeyAuthn(), {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}),
        (JwtOidcAuthn(), {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}),
    ],
    ids=lambda v: getattr(v, "name", None),
)
def test_each_authenticator_tells_the_agent_card_what_a_caller_presents(provider, schemes):
    assert provider.a2a_security() == schemes


# ---------------------------------------------------------------- rules authz


RULES = [
    {"effect": "deny", "subjects": ["mallory"], "reason": "blocked caller"},
    {
        "effect": "require_approval",
        "roles": ["officer"],
        "actions": ["tool.call"],
        "resources": ["open_*"],
        "approvers": {"roles": ["lead"]},
    },
    {"effect": "allow", "roles": ["officer", "reviewer"], "actions": ["tool.*", "model.call"]},
    {"actions": ["run.start"]},
]


async def test_the_first_matching_rule_decides():
    authz = RulesAuthz({"rules": RULES})
    blocked = await authz.check(Principal("mallory", roles=("officer",)), "tool.call", {"id": "lookup"}, {})
    assert (blocked.effect, blocked.reason) == ("deny", "blocked caller")
    approval = await authz.check(ANA, "tool.call", {"id": "open_account"}, {})
    assert (approval.effect, approval.approvers) == ("require_approval", {"roles": ["lead"]})
    allowed = await authz.check(ANA, "tool.call", {"id": "lookup"}, {})
    assert (allowed.effect, allowed.reason) == ("allow", "rule matched")


async def test_a_rule_without_an_effect_allows_and_matches_any_role_subject_or_resource():
    decision = await RulesAuthz({"rules": RULES}).check(Principal("bob"), "run.start", {"id": "anything"}, {})
    assert decision.effect == "allow"


async def test_the_resource_name_is_matched_when_it_has_no_id():
    authz = RulesAuthz({"rules": [{"effect": "deny", "resources": ["open_account"]}], "default": "allow"})
    assert (await authz.check(ANA, "tool.call", {"name": "open_account"}, {})).effect == "deny"
    assert (await authz.check(ANA, "tool.call", {"name": "lookup"}, {})).effect == "allow"


@pytest.mark.parametrize(("default", "effect"), [({}, "deny"), ({"default": "allow"}, "allow")])
async def test_when_no_rule_matches_the_default_applies_and_it_is_deny_unless_set(default, effect):
    guest = Principal("bob", roles=("guest",))
    decision = await RulesAuthz({"rules": RULES, **default}).check(guest, "tool.call", {"id": "x"}, {})
    assert (decision.effect, decision.reason) == (effect, "no rule matched")


async def test_rules_from_a_file_in_the_export_come_before_inline_rules(tmp_path):
    (tmp_path / "policy.yaml").write_text("rules:\n  - effect: deny\n    actions: ['tool.call']\n    reason: from file\n")
    authz = RulesAuthz({"file": "policy.yaml", "rules": [{"effect": "allow"}]}, BuildContext(export_root=tmp_path))
    assert (await authz.check(ANA, "tool.call", {"id": "x"}, {})).reason == "from file"
    assert (await authz.check(ANA, "model.call", {"id": "x"}, {})).effect == "allow"
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert RulesAuthz({"file": str(empty)}).rules == []  # an absolute path; an empty file has no rules


async def test_allow_all_and_mock_authz_decide_as_configured():
    assert (await AllowAllAuthz().check(ANA, "tool.call", {}, {})).effect == "allow"
    mock = MockAuthz({"deny_actions": ["tool.call:open_*"], "approval_actions": ["a2a.*"], "approvers": {"roles": ["lead"]}})
    assert (await mock.check(ANA, "tool.call", {"id": "open_account"}, {})).effect == "deny"
    approval = await mock.check(ANA, "a2a.call", {"id": "world_bank"}, {})
    assert (approval.effect, approval.approvers) == ("require_approval", {"roles": ["lead"]})
    assert (await mock.check(ANA, "tool.call", {"id": "lookup"}, {})).effect == "allow"
    assert mock.checks == [("tool.call", "open_account"), ("a2a.call", "world_bank"), ("tool.call", "lookup")]


# ---------------------------------------------------------------- http pdp authz


def pdp(endpoint: Endpoint, **settings) -> HttpPdpAuthz:
    """An HttpPdpAuthz that reaches ``endpoint`` in process."""
    authz = HttpPdpAuthz({"url": "http://pdp.test/decide", **settings}, ctx(pdp_token="pdp-secret"))
    authz.transport = httpx.MockTransport(endpoint)
    return authz


async def test_the_pdp_is_asked_with_the_principal_action_resource_and_context():
    endpoint = Endpoint({"effect": "allow", "reason": "officer may", "obligations": ["audit"]})
    authz = pdp(endpoint, headers={"X-Caller": "kyc-agent"}, token_secret="pdp_token")
    decision = await authz.check(ANA, "tool.call", {"id": "lookup"}, {"run": "r1"})
    assert (decision.effect, decision.reason, decision.obligations) == ("allow", "officer may", ("audit",))
    request = endpoint.requests[0]
    assert (request.method, str(request.url)) == ("POST", "http://pdp.test/decide")
    assert request.headers["authorization"] == "Bearer pdp-secret" and request.headers["x-caller"] == "kyc-agent"
    body = {"principal": ANA.public(), "action": "tool.call", "resource": {"id": "lookup"}, "context": {"run": "r1"}}
    assert httpx.Response(200, content=request.content).json() == body


@pytest.mark.parametrize(
    ("reply", "effect"),
    [
        ({"effect": "deny", "reason": "no"}, "deny"),
        ({"effect": "require_approval", "approvers": {"roles": ["lead"]}}, "require_approval"),
        ({"allow": True}, "allow"),
        ({"allow": False}, "deny"),
        ({}, "deny"),
    ],
)
async def test_the_pdp_answer_maps_to_a_decision(reply, effect):
    decision = await pdp(Endpoint(reply)).check(ANA, "tool.call", {"id": "x"}, {})
    assert decision.effect == effect
    assert decision.approvers == reply.get("approvers")


async def test_without_a_token_secret_the_pdp_gets_no_authorization_header():
    endpoint = Endpoint({"allow": True})
    await pdp(endpoint).check(ANA, "tool.call", {"id": "x"}, {})
    assert "authorization" not in endpoint.requests[0].headers


@pytest.mark.parametrize(
    ("endpoint", "error"),
    [
        (Endpoint({"error": "boom"}, status=500), httpx.HTTPStatusError),
        (Endpoint(error=httpx.ConnectError("refused")), httpx.ConnectError),
    ],
    ids=["error status", "unreachable"],
)
async def test_a_failing_pdp_raises_and_the_guard_fails_closed_unless_told_otherwise(endpoint, error):
    authz = pdp(endpoint)
    with pytest.raises(error):
        await authz.check(ANA, "tool.call", {"id": "x"}, {})
    closed = await Guard(authz, Tracer()).check(ANA, "tool.call", {"id": "x"})
    assert (closed.effect, closed.reason) == ("deny", f"authorization unavailable: {error.__name__}")
    opened = await Guard(authz, Tracer(), fail_closed=False).check(ANA, "tool.call", {"id": "x"})
    assert opened.effect == "allow" and opened.reason.endswith("fail_open")


# ---------------------------------------------------------------- passthrough, static secret credentials


async def test_no_credentials_never_adds_a_header():
    credential = await NoCredentials().get("tool:lookup", ANA)
    assert (dict(credential.headers), credential.expires_at) == ({}, None)


async def test_passthrough_forwards_the_callers_own_token_to_matching_targets_only():
    caller = Principal("ana", token_ref=TokenVault.put("caller-token"))
    creds = PassthroughCredentials({"targets": ["a2a:*"]})
    assert (await creds.get("a2a:world_bank", caller)).headers == {"Authorization": "Bearer caller-token"}
    assert (await creds.get("tool:lookup", caller)).headers == {}


async def test_passthrough_header_is_configurable():
    caller = Principal("ana", token_ref=TokenVault.put("caller-token"))
    assert (await PassthroughCredentials({"header": "X-Forwarded-Token"}).get("x", caller)).headers == {
        "X-Forwarded-Token": "Bearer caller-token"
    }


async def test_passthrough_adds_nothing_when_the_caller_has_no_live_token(clock):
    expired = Principal("ana", token_ref=TokenVault.put("old", ttl=5))
    clock.now += 10
    for caller in (ANA, expired, Principal("ana", token_ref="tok_unknown")):
        assert (await PassthroughCredentials().get("tool:lookup", caller)).headers == {}


async def test_static_secret_headers_come_from_the_first_matching_target_pattern():
    mappings = {"tool:registry*": {"X-Api-Key": "registry_key"}, "tool:*": {"Authorization": "generic_token"}}
    creds = StaticSecretCredentials({"mappings": mappings}, ctx(registry_key="r-1", generic_token="Bearer g-1"))
    assert (await creds.get("tool:registry_lookup", ANA)).headers == {"X-Api-Key": "r-1"}
    assert (await creds.get("tool:other", ANA)).headers == {"Authorization": "Bearer g-1"}
    assert (await creds.get("a2a:world_bank", ANA)).headers == {}


async def test_a_static_secret_that_is_missing_is_an_error_naming_it():
    creds = StaticSecretCredentials({"mappings": {"*": {"X-Api-Key": "registry_key"}}}, ctx())
    with pytest.raises(SecretNotFound, match="'registry_key'"):
        await creds.get("tool:lookup", ANA)


# ---------------------------------------------------------------- oauth client credentials


def client_credentials(**settings) -> OAuthClientCredentials:
    """An OAuthClientCredentials whose client secret is 'cs-1'."""
    base = {"token_url": "https://idp.test/token", "client_id": "kyc-agent", "client_secret_name": "client_secret"}
    return OAuthClientCredentials({**base, **settings}, ctx(client_secret="cs-1"))


async def test_a_client_credentials_token_is_fetched_with_the_client_secret_scope_and_audience(monkeypatch, clock):
    endpoint = oauth_endpoint(monkeypatch, Endpoint({"access_token": "svc-1", "expires_in": 600}))
    credential = await client_credentials(scope="registry.read", audience="registry").get("tool:lookup", ANA)
    assert (credential.headers, credential.expires_at) == ({"Authorization": "Bearer svc-1"}, clock.now + 600)
    assert str(endpoint.requests[0].url) == "https://idp.test/token"
    assert endpoint.form() == {
        "grant_type": "client_credentials",
        "client_id": "kyc-agent",
        "client_secret": "cs-1",
        "scope": "registry.read",
        "audience": "registry",
    }


async def test_scope_and_audience_are_left_out_when_not_set(monkeypatch, clock):
    endpoint = oauth_endpoint(monkeypatch, Endpoint({"access_token": "svc-1"}))
    credential = await client_credentials().get("tool:lookup", ANA)
    assert set(endpoint.form()) == {"grant_type", "client_id", "client_secret"}
    assert credential.expires_at == clock.now + 300  # no expires_in: five minutes


async def test_a_client_credentials_token_is_cached_until_30_seconds_before_it_expires(monkeypatch, clock):
    endpoint = oauth_endpoint(monkeypatch, Endpoint({"access_token": "svc-1", "expires_in": 600}))
    creds = client_credentials()
    first = await creds.get("tool:a", ANA)
    clock.now += 569
    assert await creds.get("tool:b", ANA) == first
    assert len(endpoint.requests) == 1
    clock.now += 2
    endpoint.reply = {"access_token": "svc-2", "expires_in": 600}
    assert (await creds.get("tool:a", ANA)).headers == {"Authorization": "Bearer svc-2"}
    assert len(endpoint.requests) == 2


async def test_client_credentials_are_only_sent_to_matching_targets(monkeypatch, clock):
    endpoint = oauth_endpoint(monkeypatch, Endpoint({"access_token": "svc-1"}))
    assert (await client_credentials(targets=["tool:registry*"]).get("a2a:world_bank", ANA)).headers == {}
    assert endpoint.requests == []


async def test_a_refused_client_credentials_request_raises_without_the_client_secret(monkeypatch, clock):
    oauth_endpoint(monkeypatch, Endpoint({"error": "invalid_client"}, status=401))
    creds = client_credentials()
    with pytest.raises(httpx.HTTPStatusError, match="401") as err:
        await creds.get("tool:lookup", ANA)
    assert "cs-1" not in str(err.value) and creds._cache is None


# ---------------------------------------------------------------- token exchange


def exchange(endpoint: Endpoint, **settings) -> TokenExchangeCredentials:
    """A TokenExchangeCredentials that reaches ``endpoint`` in process."""
    base = {"token_url": "https://idp.test/token", "client_id": "kyc-agent"}
    creds = TokenExchangeCredentials({**base, **settings}, ctx(client_secret="cs-1"))
    creds.transport = httpx.MockTransport(endpoint)
    return creds


async def test_the_callers_token_is_exchanged_for_one_valid_at_the_target(clock):
    endpoint = Endpoint({"access_token": "obo-1", "expires_in": 120})
    creds = exchange(endpoint, client_secret_name="client_secret", audience_map={"a2a:*": "world-bank", "*": "fallback"})
    caller = Principal("ana", token_ref=TokenVault.put("caller-token"))
    credential = await creds.get("a2a:world_bank", caller, scope="country.read")
    assert (credential.headers, credential.expires_at) == ({"Authorization": "Bearer obo-1"}, clock.now + 120)
    assert endpoint.form() == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": "caller-token",
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "client_id": "kyc-agent",
        "client_secret": "cs-1",
        "audience": "world-bank",
        "scope": "country.read",
    }


async def test_a_public_client_exchange_sends_no_secret_audience_or_scope(clock):
    endpoint = Endpoint({"access_token": "obo-1"})
    caller = Principal("ana", token_ref=TokenVault.put("caller-token"))
    credential = await exchange(endpoint).get("tool:lookup", caller)
    assert set(endpoint.form()) == {"grant_type", "subject_token", "subject_token_type", "client_id"}
    assert credential.expires_at == clock.now + 300


async def test_every_exchange_goes_to_the_token_endpoint_because_nothing_is_cached():
    endpoint = Endpoint({"access_token": "obo-1"})
    creds, caller = exchange(endpoint), Principal("ana", token_ref=TokenVault.put("caller-token"))
    await creds.get("tool:lookup", caller)
    await creds.get("tool:lookup", caller)
    assert len(endpoint.requests) == 2


async def test_no_exchange_happens_for_an_unmatched_target_or_a_caller_without_a_token():
    endpoint = Endpoint({"access_token": "obo-1"})
    creds = exchange(endpoint, targets=["a2a:*"])
    assert (await creds.get("tool:lookup", Principal("ana", token_ref=TokenVault.put("t")))).headers == {}
    assert (await creds.get("a2a:world_bank", ANA)).headers == {}
    assert endpoint.requests == []


async def test_a_refused_exchange_raises_without_the_callers_token():
    creds = exchange(Endpoint({"error": "invalid_grant"}, status=400))
    with pytest.raises(httpx.HTTPStatusError, match="400") as err:
        await creds.get("tool:lookup", Principal("ana", token_ref=TokenVault.put("caller-token")))
    assert "caller-token" not in str(err.value)


# ---------------------------------------------------------------- fail closed


@pytest.mark.parametrize(("reply", "effect"), [({"effect": "DENY"}, "deny"), ({"effect": "forbid"}, "deny"), ({"allow": "false"}, "deny")])
async def test_a_pdp_answer_spelt_another_way_is_a_deny(reply, effect):
    """Every check blocks only on "deny": an effect in capitals, an unknown effect or a string allow flag must not pass."""
    assert (await pdp(Endpoint(reply)).check(ANA, "tool.call", {"id": "lookup"}, {})).effect == effect


def test_an_unknown_effect_becomes_a_deny_that_names_it():
    """Decision normalises its effect, so a provider written elsewhere fails closed too."""
    assert (Decision("Allow").effect, Decision("require_approval").effect) == ("allow", "require_approval")
    decision = Decision("forbid", "policy 7")  # type: ignore[arg-type]
    assert (decision.effect, decision.reason) == ("deny", "unknown effect 'forbid': policy 7")


@pytest.mark.parametrize("settings", [{"default": "Deny"}, {"rules": [{"effect": "block", "roles": ["x"]}]}])
def test_rules_with_an_effect_that_is_not_known_do_not_start(settings):
    """A typo in the rules (default "Deny", effect "block") is a configuration error, not an allow."""
    with pytest.raises(ConfigError, match="effects must be one of"):
        RulesAuthz(settings)


async def test_a_token_without_an_expiry_is_rejected(jwks):
    """A token with no exp claim would never expire, so both verifiers refuse it."""
    claims = {k: v for k, v in oidc_claims().items() if k != "exp"}
    with pytest.raises(AuthenticationError, match="MissingRequiredClaimError"):
        await oidc().authenticate(bearer(signed(claims, expiring=False)))
    with pytest.raises(AuthenticationError, match="MissingRequiredClaimError"):
        await KeycloakStaticKeyAuthn({"public_key": PUBLIC_PEM}).authenticate(bearer(signed({"sub": "ana"}, expiring=False)))


async def test_an_api_key_held_under_a_later_secret_works_when_an_earlier_one_is_unset():
    """One unset key secret does not lock out the callers whose key is set."""
    authn = ApiKeyAuthn({"key_secrets": {"retired_key": "old", "partner_key": "partner"}}, ctx(partner_key="p-1"))
    assert (await authn.authenticate({"headers": {"x-api-key": "p-1"}})).subject == "partner"
