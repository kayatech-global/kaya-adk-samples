"""`runtime.yaml` per entity: one prompt, model or tool served by another provider, fields overridden, providers added by plugin."""

import sys
import textwrap
import types
from pathlib import Path

import pytest
import yaml
from google.adk.models import Gemini
from retail_kyc_onboarding._engine.app import FlowSettings
from retail_kyc_onboarding._engine.datatypes import CallContext, Principal
from retail_kyc_onboarding._engine.engine.units import ContentUnit
from retail_kyc_onboarding._engine.errors import ConfigError
from retail_kyc_onboarding._engine.factories.base import BuildContext
from retail_kyc_onboarding._engine.factories.routed import (
    RoutedApi,
    RoutedModels,
    RoutedPrompts,
)
from retail_kyc_onboarding._engine.factories.secret import MockSecrets
from retail_kyc_onboarding._engine.factories.security import NoCredentials
from retail_kyc_onboarding._engine.profile import profile_providers
from retail_kyc_onboarding.workflow import WORKFLOW
from retail_kyc_onboarding.workflow.components import models

ENV: dict[str, str] = {}
RUNTIME = Path(__file__).resolve().parents[2] / "retail_kyc_onboarding" / "runtime.yaml"
OTHER_PROMPTS = {"kyc_intake_agent": "from the other source", "kyc_risk_agent_prompt": "from the other source too"}


def built() -> dict:
    return {"secret": MockSecrets(), "credentials": NoCredentials()}


def profile_file(tmp_path: Path, content: dict, factories: dict | None = None, plugins: list | None = None) -> Path:
    """The shipped file plus a profile `custom` whose prompts, models and tools come from another source than the default's."""
    file = yaml.safe_load(RUNTIME.read_text())
    file["mode"] = "custom"
    file["plugins"] = plugins or []
    file["provider-profile"]["custom"] = {
        "factories": {"prompt": {"provider": "mock", "prompts": OTHER_PROMPTS}, "model": "mock", **(factories or {})},
        "content": content,
    }
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(file))
    return path


def test_all_applies_to_every_kind_that_has_the_provider_and_later_lines_win(tmp_path):
    path = profile_file(tmp_path, {}, {"all": "mock", "authz": "allow_all"})
    p = profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)
    assert (p.authz.name, p.authn.name, p.model.name, p.secret.name) == ("allow_all", "mock", "mock", "mock")
    assert p.session.name == "uri"  # no `mock` session provider: inherited from default


def test_a_prompt_can_stay_local_while_the_rest_come_from_another_source(tmp_path):
    path = profile_file(tmp_path, {"prompts": {"kyc_risk_agent_prompt": {"provider": "content"}}})
    p = profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)
    assert isinstance(p.prompt, RoutedPrompts)
    assert (p.prompt.default.name, p.prompt.routes["kyc_risk_agent_prompt"].name) == ("mock", "content")
    assert p.prompt.describe()["routes"]["kyc_risk_agent_prompt"]["provider"] == "content"


async def test_routed_prompts_ask_the_provider_of_that_prompt(tmp_path):
    path = profile_file(tmp_path, {"prompts": {"kyc_risk_agent_prompt": {"provider": "content"}}})
    p = profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)
    p.prompt.bind(BuildContext(env={}, built=built(), content_units={"": ContentUnit.from_content(WORKFLOW)}))
    assert (await p.prompt.get("kyc_intake_agent")).text == "from the other source"
    assert (await p.prompt.get("kyc_risk_agent_prompt")).text.startswith("You are the KYC Risk Agent")  # local


def test_a_model_can_be_served_directly_while_the_rest_come_from_another_source(tmp_path):
    path = profile_file(tmp_path, {"models": {"gpt_5_5": {"provider": "litellm"}}})
    p = profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)
    assert isinstance(p.model, RoutedModels)
    assert (p.model.pick("gemini_3_8_flash").name, p.model.pick("gpt_5_5").name) == ("mock", "litellm")


async def test_routed_models_build_each_model_with_its_own_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    path = profile_file(tmp_path, {"models": {"gemini_3_5_flash": {"provider": "litellm"}}})
    p = profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)
    p.model.bind(BuildContext(built=built()))
    assert isinstance(await p.model.create(models.GEMINI_3_5_FLASH), Gemini)  # litellm builds ADK's own Gemini class
    assert type(await p.model.create(models.GEMINI_3_8_FLASH)).__name__ == "MockLlm"  # the profile's own provider


async def test_a_tool_can_be_served_by_another_provider_while_the_rest_are_called_directly(tmp_path):
    path = profile_file(tmp_path, {"tools": {"retail_kyc_id_registry_lookup": {"provider": "mock"}}})
    p = profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)
    assert isinstance(p.api, RoutedApi)
    registry = WORKFLOW.tools["retail_kyc_id_registry_lookup"]
    p.api.bind(BuildContext(built=built()))
    assert type(p.api.build(registry)).__name__ == "MockToolHandle"
    assert type(p.api.build(WORKFLOW.tools["retail_kyc_account_creation"])).__name__ == "HttpToolHandle"
    assert (await p.api.build(registry).invoke({"id_number": "x"}, CallContext(Principal("a")))).ok


def test_field_overrides_need_no_code(tmp_path):
    (tmp_path / "intake.md").write_text("Intake from a file.", encoding="utf-8")
    path = profile_file(
        tmp_path,
        {
            "prompts": {"kyc_intake_agent": {"file": "intake.md", "version": "v-file"}, "kyc_risk_agent_prompt": {"text": "Risk, short."}},
            "models": {"gemini_3_8_flash": {"model_name": "gemini-3.9-flash", "fallbacks": ["gpt_5_5"], "api_key_secret": "my_key"}},
            "tools": {"retail_kyc_account_creation": {"config": {"timeout": 5}, "description": "Opens it."}},
        },
    )
    p = profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)
    unit = ContentUnit.from_content(WORKFLOW, p.overrides)
    assert (unit.prompts["kyc_intake_agent"].text, unit.prompts["kyc_intake_agent"].version) == ("Intake from a file.", "v-file")
    assert unit.prompts["kyc_risk_agent_prompt"].text == "Risk, short."
    spec = unit.models["gemini_3_8_flash"]
    assert (spec.model_name, spec.fallbacks, spec.api_key_secret, spec.provider) == ("gemini-3.9-flash", ("gpt_5_5",), "my_key", "google")
    tool = unit.tool_defs["retail_kyc_account_creation"]
    assert (tool.config["timeout"], tool.config["method"], tool.description) == (5, "POST", "Opens it.")  # a patch, not a replacement
    assert {"prompt:kyc_intake_agent (override)", "model:gemini_3_8_flash (override)", "tool:retail_kyc_account_creation (override)"} <= set(
        unit.modified_entities
    )


def test_an_unknown_id_lists_the_known_ones(tmp_path):
    path = profile_file(tmp_path, {"prompts": {"kyc_intake": {"text": "x"}}})
    with pytest.raises(ConfigError, match=r"content.prompts.kyc_intake: unknown prompt id. Known: kyc_intake_agent, kyc_risk_agent_prompt"):
        profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)


def test_an_unknown_field_lists_the_valid_ones(tmp_path):
    path = profile_file(tmp_path, {"models": {"gpt_5_5": {"modell_name": "x"}}})
    with pytest.raises(ConfigError, match=r"unknown field \['modell_name'\]. Valid: .*model_name"):
        profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)


def test_an_unknown_content_kind_is_an_error(tmp_path):
    path = profile_file(tmp_path, {"conditions": {}})
    with pytest.raises(ConfigError, match="unknown kind 'conditions'. Kinds: prompts, models, tools"):
        profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)


def test_a_provider_written_in_a_plugin_is_chosen_from_the_yaml_alone(tmp_path, monkeypatch):
    module = types.ModuleType("acme_plugin")
    exec(  # noqa: S102
        textwrap.dedent(
            """
            from retail_kyc_onboarding._engine.datatypes import PromptSpec
            from retail_kyc_onboarding._engine.factories.prompt import PromptFactory
            from retail_kyc_onboarding._engine.factories.registry import register

            @register
            class AcmePrompts(PromptFactory):
                name = "acme"
                async def get(self, prompt_id, version=None, scope=""):
                    return PromptSpec(prompt_id, "acme-1", "acme " + prompt_id, {}, "sha256:x")
            """
        ),
        module.__dict__,
    )
    monkeypatch.setitem(sys.modules, "acme_plugin", module)
    path = profile_file(tmp_path, {"prompts": {"kyc_intake_agent": {"provider": "acme"}}}, plugins=["acme_plugin"])
    p = profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)
    assert p.prompt.routes["kyc_intake_agent"].name == "acme"


def test_a_plugin_that_cannot_be_imported_is_an_error(tmp_path):
    path = profile_file(tmp_path, {}, plugins=["no_such_plugin_module"])
    with pytest.raises(ConfigError, match="plugins: cannot import 'no_such_plugin_module'"):
        profile_providers(FlowSettings(), path, env=ENV, content=WORKFLOW)


def test_code_in_settings_still_wins_over_the_yaml_content(tmp_path, monkeypatch):
    from retail_kyc_onboarding.settings import build_config

    path = profile_file(tmp_path, {"prompts": {"kyc_intake_agent": {"text": "from yaml"}, "kyc_risk_agent_prompt": {"text": "risk yaml"}}})
    monkeypatch.setenv("FLOW_RUNTIME_FILE", str(path))
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    config = build_config(prompts={"kyc_intake_agent": "from code"})
    assert config.overrides.prompts["kyc_intake_agent"] == "from code"
    assert config.overrides.prompts["kyc_risk_agent_prompt"].text == "risk yaml"
