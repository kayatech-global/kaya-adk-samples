"""Every factory kind resolves to a working provider in both shipped profiles, and `static` ADK plugins can be named in the YAML."""

import sys
import types
from pathlib import Path

import pytest
from google.adk.plugins.base_plugin import BasePlugin
from retail_kyc_onboarding._engine.app import FlowSettings
from retail_kyc_onboarding._engine.errors import ConfigError
from retail_kyc_onboarding._engine.factories.adk_plugin import StaticAdkPlugins
from retail_kyc_onboarding._engine.factories.base import Provider
from retail_kyc_onboarding._engine.factories.config import BUILD_ORDER
from retail_kyc_onboarding._engine.profile import profile_providers
from retail_kyc_onboarding.workflow import WORKFLOW

RUNTIME = Path(__file__).resolve().parents[2] / "retail_kyc_onboarding" / "runtime.yaml"
ENV: dict[str, str] = {}
ARC_KINDS = {"secret", "credentials", "authn", "authz", "prompt", "model", "api", "mcp"}  # the placeholders of arc_providers.py


@pytest.mark.parametrize("mode", ["default", "arc"])
def test_every_kind_resolves_in_both_profiles(mode, monkeypatch):
    monkeypatch.setenv("FLOW_PROVIDER_MODE", mode)
    p = profile_providers(FlowSettings(), RUNTIME, env=ENV, content=WORKFLOW)
    for kind in BUILD_ORDER:
        provider = getattr(p, kind)
        assert isinstance(provider, Provider) and provider.kind == kind, kind
    arc = {kind for kind in BUILD_ORDER if getattr(p, kind).name == "arc"}
    assert arc == (ARC_KINDS if mode == "arc" else set())
    assert p.mode == mode


class AuditPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="audit")


async def test_static_adk_plugins_are_named_by_class_path_in_the_yaml(tmp_path, monkeypatch):
    module = types.ModuleType("acme_plugins")
    module.AuditPlugin = AuditPlugin
    monkeypatch.setitem(sys.modules, "acme_plugins", module)
    path = tmp_path / "runtime.yaml"
    path.write_text(
        "mode: default\nprovider-profile:\n  default:\n    factories:\n"
        "      adk_plugin: {provider: static, plugins: ['acme_plugins:AuditPlugin']}\n"
    )
    p = profile_providers(FlowSettings(), path, env={}, content=WORKFLOW)
    assert isinstance(p.adk_plugin, StaticAdkPlugins)
    assert [x.name for x in await p.adk_plugin.plugins(None)] == ["audit"]
    assert p.adk_plugin.describe()["plugins"] == ["audit"]


def test_static_adk_plugins_still_take_instances_in_code():
    assert StaticAdkPlugins([AuditPlugin()]).describe()["plugins"] == ["audit"]


def test_a_plugin_class_that_cannot_be_imported_is_named(tmp_path):
    path = tmp_path / "runtime.yaml"
    path.write_text("mode: default\nprovider-profile:\n  default:\n    factories:\n      adk_plugin: {provider: static, plugins: ['nope:Missing']}\n")
    with pytest.raises(ConfigError, match="adk plugin 'nope:Missing' cannot be built"):
        profile_providers(FlowSettings(), path, env={}, content=WORKFLOW)
