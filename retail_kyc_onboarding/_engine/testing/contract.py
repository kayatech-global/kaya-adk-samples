"""Contract kits: what every provider of a factory kind must satisfy. Run them against your own providers too:

from <package>._engine.testing.contract import check_secret_provider
await check_secret_provider(MySecrets(...), known={"name": "value"})
"""

from __future__ import annotations

from typing import Any

from ..datatypes import ANONYMOUS, Credential, Decision, Principal, PromptSpec
from ..errors import AuthenticationError
from ..factories.prompt import PromptFactory, PromptNotFound
from ..factories.router import RouterInput, RouterStrategy
from ..factories.secret import SecretFactory, SecretNotFound
from ..factories.security import Authenticator, Authorizer, CredentialProvider


async def check_secret_provider(p: SecretFactory, known: dict[str, str]) -> None:
    """Assert a secret provider returns known values, raises on unknown names, is healthy and does not leak values in ``describe()``."""
    for name, value in known.items():
        assert await p.get(name) == value
    try:
        await p.get("definitely-not-a-secret-name")
    except SecretNotFound:
        pass
    else:
        raise AssertionError("missing secret must raise SecretNotFound")
    assert (await p.health()).ok
    desc = p.describe()
    assert desc["kind"] == "secret"
    assert not any(v in str(desc) for v in known.values()), "describe() must not leak secret values"


async def check_prompt_provider(p: PromptFactory, known: dict[str, str]) -> None:
    """Assert a prompt provider returns known prompts with a version and hash, and raises ``PromptNotFound`` for unknown ones."""
    for pid, text in known.items():
        spec = await p.get(pid)
        assert isinstance(spec, PromptSpec) and spec.text == text and spec.version
        assert spec.sha256.startswith("sha256:")
    try:
        await p.get("no-such-prompt")
    except PromptNotFound:
        pass
    else:
        raise AssertionError("missing prompt must raise PromptNotFound")


async def check_authenticator(p: Authenticator, good: dict[str, Any], bad: dict[str, Any], expect_subject: str) -> None:
    """Assert an authenticator maps good credentials to the expected subject and raises ``AuthenticationError`` for bad ones."""
    principal = await p.authenticate(good)
    assert isinstance(principal, Principal) and principal.subject == expect_subject
    try:
        await p.authenticate(bad)
    except AuthenticationError:
        pass
    else:
        raise AssertionError("a bad credential must raise AuthenticationError")


async def check_authorizer(
    p: Authorizer, allowed: tuple[Principal, str, dict[str, Any]], denied: tuple[Principal, str, dict[str, Any]]
) -> None:
    """Assert an authorizer allows the allowed case and denies or escalates the denied one."""
    ok = await p.check(allowed[0], allowed[1], allowed[2], {})
    no = await p.check(denied[0], denied[1], denied[2], {})
    assert isinstance(ok, Decision) and ok.effect == "allow", ok
    assert no.effect in {"deny", "require_approval"}, no


async def check_credentials(p: CredentialProvider) -> None:
    """Assert a credential provider returns a ``Credential`` with no secret-like header names."""
    cred = await p.get("tool:contract", ANONYMOUS, None)
    assert isinstance(cred, Credential)
    assert not any("secret" in k.lower() for k in cred.headers), "never put secrets in header names"


async def check_router(p: RouterStrategy, inp: RouterInput, allowed_or_finish: set[str | None]) -> None:
    """Assert a router strategy picks one of the allowed next nodes, or the invalid marker."""
    choice = await p.choose(inp)
    assert choice.next_node in allowed_or_finish or choice.next_node == "__invalid__", choice


async def check_adk_plugin_provider(p: Any, engine: Any) -> None:
    """Contract kit for kind ``adk_plugin``: ADK plugin instances with unique, non-empty names, none called ``flow_trace``.

    Run it against your own provider: ``await check_adk_plugin_provider(MyPlugins(settings, ctx), engine)``.
    """
    from google.adk.plugins.base_plugin import BasePlugin

    from ..factories.adk_plugin import RESERVED_NAMES, AdkPluginFactory

    assert isinstance(p, AdkPluginFactory), "an adk_plugin provider must subclass AdkPluginFactory"
    plugins = await p.plugins(engine)
    assert isinstance(plugins, list) and all(isinstance(x, BasePlugin) for x in plugins), "plugins() must return BasePlugin instances"
    names = [x.name for x in plugins]
    assert all(names) and len(set(names)) == len(names), f"plugin names must be unique and non-empty: {names}"
    assert not set(names) & RESERVED_NAMES, "a plugin may not take the engine's own name"
    assert (await p.health()).ok
