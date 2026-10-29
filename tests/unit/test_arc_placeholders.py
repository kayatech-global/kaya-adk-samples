"""The `arc` providers are placeholders: the shipped `arc` profile selects them, and each says it is not implemented yet."""

from pathlib import Path

import pytest
from retail_kyc_onboarding import arc_providers
from retail_kyc_onboarding._engine.app import FlowSettings
from retail_kyc_onboarding._engine.datatypes import ModelSpec, Principal, ToolDef
from retail_kyc_onboarding._engine.factories.config import BUILD_ORDER
from retail_kyc_onboarding._engine.profile import profile_providers
from retail_kyc_onboarding.workflow import WORKFLOW

RUNTIME = Path(__file__).resolve().parents[2] / "retail_kyc_onboarding" / "runtime.yaml"
PLACEHOLDERS = {
    "secret": (arc_providers.ArcSecrets, "get", ("arc_api_key",)),
    "authn": (arc_providers.ArcAuthn, "authenticate", ({"headers": {}},)),
    "authz": (arc_providers.ArcAuthz, "check", (Principal("a"), "run.start", {}, {})),
    "credentials": (arc_providers.ArcCredentials, "get", ("tool:x", Principal("a"))),
    "prompt": (arc_providers.ArcPrompts, "get", ("kyc_intake_agent",)),
    "model": (arc_providers.ArcModels, "create", (ModelSpec(id="m"),)),
    "api": (arc_providers.ArcApiFactory, "build", (ToolDef(name="t", type="api"),)),
    "mcp": (arc_providers.ArcMcpFactory, "build", (ToolDef(name="t", type="mcp"),)),
}


def test_the_arc_profile_selects_every_placeholder_and_keeps_the_rest_default():
    p = profile_providers(FlowSettings(provider_mode="arc"), RUNTIME, env={}, content=WORKFLOW)
    assert {kind for kind in BUILD_ORDER if getattr(p, kind).name == "arc"} == set(PLACEHOLDERS)
    assert {kind: type(getattr(p, kind)) for kind in PLACEHOLDERS} == {kind: cls for kind, (cls, _, _) in PLACEHOLDERS.items()}
    assert (p.session.name, p.telemetry.name) == ("uri", "console")


@pytest.mark.parametrize("kind", sorted(PLACEHOLDERS))
async def test_a_placeholder_says_it_is_not_implemented(kind):
    cls, method, args = PLACEHOLDERS[kind]
    provider = cls()
    with pytest.raises(NotImplementedError, match=rf"arc {kind} provider: {cls.__name__}.{method} is a placeholder; implement it in arc_providers.py"):
        result = getattr(provider, method)(*args)
        if hasattr(result, "__await__"):
            await result
    health = await provider.health()
    assert not health.ok and "placeholder" in health.detail


def test_the_default_mode_does_not_use_them():
    p = profile_providers(FlowSettings(provider_mode="default"), RUNTIME, env={}, content=WORKFLOW)
    assert not [kind for kind in BUILD_ORDER if getattr(p, kind).name == "arc"]
