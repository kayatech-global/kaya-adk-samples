"""Every worked example of the configuration how-tos runs as written, so the docs cannot drift from the code.

Each test is named after the section it checks. The examples are read out of the Markdown files, not copied here.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml
from retail_kyc_onboarding._engine.app import (
    AppConfig,
    FlowSettings,
    Overrides,
    default_providers,
)
from retail_kyc_onboarding._engine.content import Prompt
from retail_kyc_onboarding._engine.engine.render import render
from retail_kyc_onboarding._engine.engine.units import ContentUnit
from retail_kyc_onboarding._engine.errors import ConfigError, ProviderNotFound
from retail_kyc_onboarding._engine.profile import (
    TOOL_FIELDS,
    profile_providers,
)
from retail_kyc_onboarding._engine.runtime.entry import build_engine
from retail_kyc_onboarding.workflow import WORKFLOW
from retail_kyc_onboarding.workflow.components import models, prompts

ROOT = Path(__file__).resolve().parents[2]  # the repository root
HOWTO = ROOT / "docs" / "how-to"
PACKAGE = "retail_kyc_onboarding"
AGENT_DIR = ROOT / PACKAGE  # the agent folder


def section(doc: Path, heading: str) -> str:
    """The text of ``## heading`` up to the next ``## `` heading."""
    text = doc.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"{doc.name} has no section '{heading}'"
    return match.group(1)


def blocks(doc: Path, heading: str, lang: str) -> list[str]:
    """The fenced ``lang`` blocks of a section, in order (indented list blocks are dedented)."""
    found = re.findall(rf"^[ ]*```{lang}\n(.*?)^[ ]*```", section(doc, heading), re.MULTILINE | re.DOTALL)
    assert found, f"{doc.name} '{heading}' has no {lang} block"
    return [re.sub(r"^ {3}", "", block, flags=re.MULTILINE) if block.startswith("   ") else block for block in found]


def write_yaml(tmp_path: Path, body: str, name: str = "runtime.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def shipped() -> dict[str, Any]:
    return yaml.safe_load((AGENT_DIR / "runtime.yaml").read_text(encoding="utf-8"))


def merged_with_shipped(tmp_path: Path, snippet: str, mode: str) -> Path:
    """The shipped file with the snippet's factories laid over the profile ``mode``."""
    file = shipped()
    for name, profile in yaml.safe_load(snippet)["provider-profile"].items():
        target = file["provider-profile"].setdefault(name, {})
        target.setdefault("factories", {}).update(profile.get("factories", {}))
    file["mode"] = mode
    return write_yaml(tmp_path, yaml.safe_dump(file))


def run_python(code: str, **namespace: Any) -> dict[str, Any]:
    """Run a doc snippet as if it were in `settings.py` (relative imports work)."""
    scope = {"__name__": f"{PACKAGE}.settings", "__package__": PACKAGE, **namespace}
    exec(compile(code, "<doc>", "exec"), scope)  # noqa: S102 - our own documentation
    return scope


def unit_with(prompts_: dict[str, Any] | None = None, models_: dict[str, Any] | None = None) -> ContentUnit:
    return ContentUnit.from_content(WORKFLOW, Overrides(prompts=prompts_ or {}, models=models_ or {}))


async def model_chain(monkeypatch, override: dict[str, Any] | None = None, **env: str):
    for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "MODEL_GPT_5_5_API_KEY", "MODEL_GEMINI_3_8_FLASH_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = FlowSettings()
    config = AppConfig(settings=settings, providers=default_providers(settings), overrides=Overrides(models=override or {}))
    engine = await build_engine(WORKFLOW, config)
    return await engine.model_for(engine.unit, "gemini_3_8_flash")


# ------------------------------------------------------------------------------------------ configure-providers.md

CONFIGURE = HOWTO / "configure-providers.md"


def test_configure_change_one_provider(tmp_path):
    p = profile_providers(FlowSettings(), write_yaml(tmp_path, blocks(CONFIGURE, "Change one provider", "yaml")[0]), env={})
    assert (p.session.name, p.session.settings.uri) == ("uri", "sqlite+aiosqlite:///runs.db")
    assert p.runstore.settings.uri == "sqlite+aiosqlite:///runs.db"
    assert (p.telemetry.name, p.telemetry.settings.path) == ("jsonl", "runs/trace.jsonl")
    assert (p.authn.name, p.authn.settings.issuer, p.authn.settings.audience) == ("jwt_oidc", "https://idp.example", "kyc-onboarding")
    assert p.secret.name == "env"  # the rest is still the default


def test_configure_give_a_provider_settings_shows_the_real_error(tmp_path):
    bad = write_yaml(tmp_path, blocks(CONFIGURE, "Give a provider settings", "yaml")[0])
    with pytest.raises(ConfigError) as err:
        profile_providers(FlowSettings(), bad, env={})
    assert str(err.value) == blocks(CONFIGURE, "Give a provider settings", "text")[0].strip()


def test_configure_use_a_provider_of_your_own(tmp_path, monkeypatch):
    module = types.ModuleType(f"{PACKAGE}.my_providers")
    module.__package__ = PACKAGE
    exec(compile(blocks(CONFIGURE, "Use a provider of your own", "python")[0], "<doc>", "exec"), module.__dict__)  # noqa: S102
    monkeypatch.setitem(sys.modules, module.__name__, module)
    p = profile_providers(FlowSettings(), write_yaml(tmp_path, blocks(CONFIGURE, "Use a provider of your own", "yaml")[0]), env={})
    assert (p.secret.name, p.secret.settings.url, p.secret.settings.token_env) == ("my_vault", "https://vault.example.internal", "VAULT_TOKEN")


def test_configure_add_your_own_mode(tmp_path):
    path = write_yaml(tmp_path, blocks(CONFIGURE, "Add your own mode", "yaml")[0])
    env = {"STAGING_SESSION_URI": "postgresql+asyncpg://db/runs"}
    p = profile_providers(FlowSettings(provider_mode="staging"), path, env=env)
    assert p.mode == "staging"
    assert (p.authn.name, p.session.settings.uri, p.telemetry.name) == ("jwt_oidc", "postgresql+asyncpg://db/runs", "console")


def test_configure_use_a_different_file(tmp_path):
    path = write_yaml(tmp_path, blocks(CONFIGURE, "Use a different file", "yaml")[0])
    p = profile_providers(FlowSettings(), path, env={})
    assert p.mode == "local" and (p.session.settings.uri, p.telemetry.name) == ("sqlite+aiosqlite:///runs.db", "jsonl")
    assert (p.secret.name, p.model.name) == ("env", "litellm")  # the built-in defaults


def test_configure_the_errors_in_the_table_are_the_real_ones(tmp_path):
    table = section(CONFIGURE, "Read the start-up errors")
    quoted = re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE)
    assert len(quoted) == 7
    arc = tmp_path / "runtime.yaml"
    cases: dict[str, Any] = {}

    def raised(body: dict[str, Any], mode: str = "", env: dict[str, str] | None = None, settings: FlowSettings | None = None) -> str:
        arc.write_text(yaml.safe_dump(body))
        with pytest.raises((ConfigError, ProviderNotFound)) as err:
            profile_providers(settings or FlowSettings(provider_mode=mode), arc, env=env or {})
        return str(err.value)

    profiles = {"default": {}, "arc": {}}
    cases["unknown mode"] = raised({"mode": "nope", "provider-profile": profiles})
    cases["unknown kind"] = raised({"provider-profile": {"default": {"factories": {"execution": "embedded"}}}})
    cases["unknown provider"] = raised({"mode": "m", "provider-profile": {"m": {"factories": {"secret": "vaultt"}}}})
    cases["missing variable"] = raised({"mode": "staging", "provider-profile": {"staging": {"config": {"url": "${env:STAGING_URL}"}}}})
    cases["circle"] = raised({"mode": "a", "provider-profile": {"a": {"extends": "b"}, "b": {"extends": "a"}}})
    with pytest.raises(ConfigError) as err:
        profile_providers(FlowSettings(provider_mode="arc"), tmp_path / "gone.yaml", env={})
    cases["no file"] = str(err.value).replace(str(tmp_path), "...")
    cases["settings"] = raised({"mode": "m", "provider-profile": {"m": {"factories": {"telemetry": {"provider": "jsonl", "pathh": "x"}}}}})
    assert len(cases) == len(quoted)
    for text, real in zip(quoted, cases.values(), strict=True):
        assert text.split("...")[0].replace("<default>", "") in real.replace("<default>", ""), (text, real)


# ------------------------------------------------------------------------------------------ update-prompts.md

PROMPT_DOC = HOWTO / "update-prompts.md"
KYC = prompts.KYC_RISK_AGENT_PROMPT


def test_prompts_replace_the_text_of_one_prompt():
    ns = run_python(blocks(PROMPT_DOC, "Replace the text of one prompt", "python")[0], PROMPTS={})
    new = ns["PROMPTS"]["kyc_risk_agent_prompt"]
    assert "GDP growth is below 1" in new and new != KYC.text
    unit = unit_with(ns["PROMPTS"])
    assert unit.prompts["kyc_risk_agent_prompt"].text == new
    assert "prompt:kyc_risk_agent_prompt (override)" in unit.modified_entities


def test_prompts_keep_the_placeholders():
    table = section(PROMPT_DOC, "Keep the placeholders")
    variables = {v.name for v in WORKFLOW.graph.variables}
    for name in re.findall(r"`\{([a-z_.]+)\}`", "\n".join(line for line in table.splitlines() if line.startswith("|"))):
        assert name in variables or name.startswith("world_bank_country_risk_agent."), f"{name} is not a variable of the workflow"
    ns = run_python(blocks(PROMPT_DOC, "Keep the placeholders", "python")[0], PROMPTS={}, KYC_RISK_AGENT_PROMPT=KYC)
    fixed = ns["PROMPTS"]["kyc_risk_agent_prompt"]
    assert "{country_risk_agent}" not in fixed
    shown = render(fixed, {"world_bank_country_risk_agent.response": "GDP growth 2.1"})
    assert "GDP growth 2.1" in shown.text and "world_bank_country_risk_agent.response" not in shown.missing
    assert "country_risk_agent" in render(KYC.text, {}).missing  # the exported prompt leaves it as written


def test_prompts_pin_a_whole_prompt_with_its_own_version():
    ns = run_python(blocks(PROMPT_DOC, "Pin a whole prompt with its own version", "python")[0], PROMPTS={}, KYC_INTAKE_AGENT=prompts.KYC_INTAKE_AGENT)
    pinned = ns["PROMPTS"]["kyc_intake_agent"]
    assert isinstance(pinned, Prompt)
    unit = unit_with({"kyc_intake_agent": pinned})
    assert unit.prompts["kyc_intake_agent"].version == "v-2026-11"
    assert unit.prompts["kyc_intake_agent"].text.endswith("never as an empty string.")


# ------------------------------------------------------------------------------------------ update-agents.md

AGENT_DOC = HOWTO / "update-agents.md"


def agent_steps() -> dict[str, dict[str, Any]]:
    graph = WORKFLOW.graph.model_dump(mode="json")
    steps: dict[str, dict[str, Any]] = {}
    for node in graph["nodes"]:
        config = node["config"]
        agent = config.get("agent") if "agent" in config else config
        if not agent or "prompt" not in agent:
            continue
        before = (agent.get("interrupt") or {}).get("before") or {}
        steps[node["id"]] = {"prompt": agent["prompt"], "model": agent["model"], "gate": bool(before.get("enabled"))}
    return steps


def test_agents_the_step_table_matches_the_graph():
    rows = re.findall(r"^\| `([a-z_]+)` \| `([a-z_]+)` \| `([a-z_0-9]+)` \| (yes|no) \|", section(AGENT_DOC, "The agent steps"), re.MULTILINE)
    assert {r[0]: {"prompt": r[1], "model": r[2], "gate": r[3] == "yes"} for r in rows} == agent_steps()
    assert len(rows) == 7


def test_agents_change_what_an_agent_decides():
    ns = run_python(blocks(AGENT_DOC, "Change what an agent decides", "python")[0], PROMPTS={})
    assert "GDP growth is below 1" in unit_with(ns["PROMPTS"]).prompts["kyc_risk_agent_prompt"].text


def test_agents_change_the_model_of_every_step():
    ns = run_python(blocks(AGENT_DOC, "Change the model of every step", "python")[0], MODELS={})
    unit = unit_with(models_=ns["MODELS"])
    assert (unit.models["gemini_3_8_flash"].model_name, unit.models["gemini_3_8_flash"].fallbacks) == ("gemini-3.5-flash", ())
    assert "model:gemini_3_8_flash (override)" in unit.modified_entities


def test_agents_run_one_step_on_a_different_model():
    assert models.GPT_5_5.id in WORKFLOW.models and models.GEMINI_3_8_FLASH.id in WORKFLOW.models
    assert 'model="gemini_3_8_flash"' in (AGENT_DIR / "workflow" / "graph.py").read_text(encoding="utf-8")


def test_agents_the_gate_snippet_is_what_the_graph_has():
    snippet = re.sub(r"\s+", "", blocks(AGENT_DOC, "Add or remove a review gate", "python")[0])
    graph = re.sub(r"\s+", "", (AGENT_DIR / "workflow" / "graph.py").read_text(encoding="utf-8"))
    assert snippet in graph


# ------------------------------------------------------------------------------------------ change-the-model.md

MODEL_DOC = HOWTO / "change-the-model.md"


def statements(code: str) -> list[str]:
    """The top-level statements of a snippet, each as source."""
    tree = ast.parse(code)
    return [ast.get_source_segment(code, node) or "" for node in tree.body]


async def test_models_add_or_remove_a_fallback(monkeypatch):
    code = blocks(MODEL_DOC, "Add or remove a fallback", "python")[0]
    imports = [s for s in statements(code) if s.startswith(("from", "import"))]
    skip_gemini, no_fallback = [s for s in statements(code) if s.startswith("MODELS[")]
    chain = []
    for assign in (skip_gemini, no_fallback):
        ns = run_python("\n".join(imports), MODELS={})
        exec(assign, ns)  # noqa: S102
        chain.append(await model_chain(monkeypatch, ns["MODELS"], GOOGLE_API_KEY="g", OPENAI_API_KEY="o"))
    assert [m.model for m in chain[0].models] == ["gemini-3.8-flash", "openai/gpt-5.5"]
    assert chain[1].model == "gemini-3.8-flash"  # a single model: no FallbackModel around it


@pytest.mark.filterwarnings("ignore:.*GEMINI_VIA_LITELLM.*:UserWarning")  # the warning the doc tells about
async def test_models_give_a_model_its_own_api_key(monkeypatch):
    code = blocks(MODEL_DOC, "Give a model its own API key", "python")[0]
    ns = run_python("from dataclasses import replace\nfrom .workflow.components.models import GEMINI_3_8_FLASH\nMODELS = {}\n" + code)
    shared = await model_chain(monkeypatch, ns["MODELS"], GOOGLE_API_KEY="g")
    assert type(shared.models[0]).__name__ == "TimedGemini"  # no key of its own set: the shared GOOGLE_API_KEY, as before
    own = await model_chain(monkeypatch, ns["MODELS"], GOOGLE_API_KEY="g", MODEL_GEMINI_3_8_FLASH_API_KEY="own-key")
    assert type(own.models[0]).__name__ == "LiteLlm" and own.models[0].model == "gemini/gemini-3.8-flash"
    assert [m.model for m in own.models][1] == "gemini-3.5-flash"  # the fallbacks still apply
    assert "ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS" in section(MODEL_DOC, "Give a model its own API key")


def test_models_use_a_gateway(tmp_path):
    path = merged_with_shipped(tmp_path, blocks(MODEL_DOC, "Use a gateway", "yaml")[0], "default")
    p = profile_providers(FlowSettings(), path, env={})
    assert p.model.name == "gateway_openai" and p.model.settings.auth_secret == "gateway_token"
    assert p.model.settings.model_map["gemini_3_8_flash"] == "gemini-3-8-flash-prod"
    assert p.model.wire_model(models.GEMINI_3_8_FLASH) == "openai/gemini-3-8-flash-prod"


def test_configure_route_or_override_one_entity(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "intake.md").write_text("From a file.", encoding="utf-8")
    snippet = blocks(CONFIGURE, "Route or override one entity", "yaml")[0]
    path = merged_with_shipped(tmp_path, snippet, "staging")
    file = yaml.safe_load(path.read_text())
    file["provider-profile"]["staging"]["content"] = yaml.safe_load(snippet)["provider-profile"]["staging"]["content"]
    path.write_text(yaml.safe_dump(file))
    p = profile_providers(FlowSettings(), path, env={}, content=WORKFLOW)
    assert p.prompt.routes["kyc_risk_agent_prompt"].name == "content" and p.model.routes["gpt_5_5"].name == "litellm"
    assert p.api.routes["retail_kyc_id_registry_lookup"].name == "http"
    unit = ContentUnit.from_content(WORKFLOW, p.overrides)
    assert unit.prompts["kyc_intake_agent"].text == "From a file."
    assert unit.models["gemini_3_8_flash"].model_name == "gemini-3.9-flash"
    assert unit.tool_defs["retail_kyc_account_creation"].config["timeout"] == 30


def test_configure_add_a_provider_class_without_touching_any_other_file(tmp_path, monkeypatch):
    snippet = yaml.safe_load(blocks(CONFIGURE, "Add a provider class without touching any other file", "yaml")[0])
    assert snippet["plugins"] == [f"{PACKAGE}.my_providers"]
    module = types.ModuleType(f"{PACKAGE}.my_providers")
    module.__package__ = PACKAGE
    exec(  # noqa: S102
        "from ._engine.factories.prompt import PromptFactory\nfrom ._engine.factories.registry import register\n"
        "@register\nclass Acme(PromptFactory):\n    name = 'acme'\n    async def get(self, prompt_id, version=None, scope=''): ...\n",
        module.__dict__,
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    path = write_yaml(tmp_path, yaml.safe_dump(snippet))
    p = profile_providers(FlowSettings(), path, env={}, content=WORKFLOW)
    assert p.prompt.routes["kyc_intake_agent"].name == "acme"


# docs/how-to/update-tools.md

TOOLS_DOC = HOWTO / "update-tools.md"
REGISTRY_ID, ACCOUNT_ID = "retail_kyc_id_registry_lookup", "retail_kyc_account_creation"


def tools_profile(tmp_path, snippet: str, mode: str = "default", env: dict[str, str] | None = None):
    """The shipped runtime.yaml with the snippet's profile keys laid over, and the providers and tool definitions it gives."""
    file, extra = shipped(), yaml.safe_load(snippet)
    file["plugins"] = extra.get("plugins", [])
    for name, profile in extra["provider-profile"].items():
        target = file["provider-profile"].setdefault(name, {})
        for key, value in profile.items():
            target[key] = {**(target.get(key) or {}), **value} if isinstance(value, dict) else value
    file["mode"] = mode
    p = profile_providers(FlowSettings(), write_yaml(tmp_path, yaml.safe_dump(file)), env=env or {}, content=WORKFLOW)
    return p, ContentUnit.from_content(WORKFLOW, p.overrides)


def http_tool(p, unit, tool_id: str, answer):
    """The tool's handle on the `http` provider, calling ``answer`` instead of the network."""
    import httpx
    from retail_kyc_onboarding._engine.factories.base import BuildContext
    from retail_kyc_onboarding._engine.factories.secret import MockSecrets
    from retail_kyc_onboarding._engine.factories.security import NoCredentials

    secrets = MockSecrets({"values": {"accounts_api_token": "tok", "accounts_client_id": "cid", "arc_api_key": "test"}})
    p.api.bind(BuildContext(built={"secret": secrets, "credentials": NoCredentials()}))
    factory = p.api.pick(tool_id) if hasattr(p.api, "pick") else p.api
    factory.transport = httpx.MockTransport(answer)
    return factory.build(unit.tool_defs[tool_id])


def call():
    from retail_kyc_onboarding._engine.datatypes import CallContext, Principal

    return CallContext(Principal("ana", roles=("compliance_officer",)), idempotency_key="idem-1")


def test_tools_the_tool_table_matches_the_workflow():
    text = section(TOOLS_DOC, "What you can change") + TOOLS_DOC.read_text(encoding="utf-8").split("## What you can change")[0]
    for tool_id, step in ((REGISTRY_ID, "look_up_id_registry"), (ACCOUNT_ID, "create_active_account")):
        node = next(n for n in WORKFLOW.graph.nodes if n.id == step)
        assert tool_id in node.config["tools"] and f"`{tool_id}`" in text and f"`{step}`" in text
        assert WORKFLOW.tools[tool_id].config["method"] in text
    assert {"description", "config", "approval", "version", "manual_execution"} <= TOOL_FIELDS


async def test_tools_point_a_tool_at_another_url(tmp_path):
    import httpx

    snippet, hosts = blocks(TOOLS_DOC, "Point a tool at another URL", "yaml")
    p, unit = tools_profile(tmp_path, snippet)
    tool = unit.tool_defs[REGISTRY_ID]
    assert (tool.config["timeout"], tool.config["retry_attempts"], tool.config["method"]) == (10, 1, "GET")
    assert "tool:retail_kyc_id_registry_lookup (override)" in unit.modified_entities
    seen: list[str] = []

    def answer(request):
        seen.append(str(request.url))
        return httpx.Response(503) if len(seen) == 1 else httpx.Response(200, json=[{"id_number": "x"}])

    result = await http_tool(p, unit, REGISTRY_ID, answer).invoke({"id_number": "x"}, call())
    assert result.ok and result.data == [{"id_number": "x"}]
    assert seen == ["https://registry.staging.example.internal/api/v1/customers?id_number=x"] * 2  # one retry after the 503

    limited, unit2 = tools_profile(tmp_path, hosts)
    assert limited.api.settings.allow_hosts == ["registry.staging.example.internal", "608b7a57737e470017b74d29.mockapi.io"]
    limited.api.check_host(unit2.tool_defs[REGISTRY_ID].config["url"])  # the exported host is allowed
    with pytest.raises(Exception, match="not in the api allow_hosts"):
        limited.api.check_host("https://elsewhere.example/x")


async def test_tools_add_authentication_to_a_tool(tmp_path):
    import httpx

    p, unit = tools_profile(tmp_path, blocks(TOOLS_DOC, "Add authentication to a tool", "yaml")[0])
    seen: list[httpx.Request] = []

    def answer(request):
        seen.append(request)
        return httpx.Response(201, json={"account_number": "AC-1"})

    args = {"customer_id_number": "1", "full_name": "A", "product": "Savings Account", "status": "ACTIVE"}
    result = await http_tool(p, unit, ACCOUNT_ID, answer).invoke(args, call())
    request = seen[0]
    assert result.ok and str(request.url) == "https://accounts.example.internal/v1/accounts" and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer tok" and request.headers["X-Client-Id"] == "cid"
    assert request.headers["Idempotency-Key"] == "idem-1" and json.loads(request.content) == args
    table = section(TOOLS_DOC, "Add authentication to a tool")
    for kind in ("no-auth", "bearer", "basic", "api-key", "oauth2"):
        assert f"| `{kind}` |" in table


async def test_tools_require_approval_before_a_tool_runs(tmp_path):
    from retail_kyc_onboarding._engine.datatypes import Principal
    from retail_kyc_onboarding._engine.factories.approval import ByToolPolicy

    p, unit = tools_profile(tmp_path, blocks(TOOLS_DOC, "Require approval before a tool runs", "yaml")[0])
    assert p.approval_policy.name == "by_tool"
    requirement = await ByToolPolicy().evaluate(unit.tool_defs[ACCOUNT_ID], {}, Principal("ana"), None, {})
    assert requirement.required and requirement.approvers == {"kind": "roles", "any_of": ["compliance_officer"]}
    assert (requirement.timeout_seconds, requirement.on_timeout) == (3600, "deny")
    untouched = await ByToolPolicy().evaluate(unit.tool_defs[REGISTRY_ID], {}, Principal("ana"), None, {})
    assert not untouched.required


async def test_tools_call_tools_some_other_way(tmp_path, monkeypatch):
    import httpx

    code = blocks(TOOLS_DOC, "Call tools some other way", "python")[0]
    module = types.ModuleType(f"{PACKAGE}.my_providers")
    module.__package__ = PACKAGE
    exec(compile(code, "<update-tools>", "exec"), module.__dict__)  # noqa: S102 - our own documentation
    monkeypatch.setitem(sys.modules, module.__name__, module)
    p, unit = tools_profile(tmp_path, blocks(TOOLS_DOC, "Call tools some other way", "yaml")[0])
    gateway = p.api.pick(ACCOUNT_ID)
    assert (gateway.name, p.api.pick(REGISTRY_ID).name, gateway.settings.url) == ("tool_gateway", "http", "https://tools.example.internal")

    from retail_kyc_onboarding._engine.factories.base import BuildContext
    from retail_kyc_onboarding._engine.factories.secret import MockSecrets

    p.api.bind(BuildContext(built={"secret": MockSecrets({"values": {"tool_gateway_token": "gw"}})}))
    seen: list[httpx.Request] = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, json={"account_number": "AC-9"})

    real = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw))
    result = await gateway.build(unit.tool_defs[ACCOUNT_ID]).invoke({"status": "ACTIVE"}, call())
    assert result.ok and result.data == {"account_number": "AC-9"}
    assert str(seen[0].url) == "https://tools.example.internal/tools/retail_kyc_account_creation"
    assert seen[0].headers["Authorization"] == "Bearer gw" and seen[0].headers["Idempotency-Key"] == "idem-1"


def test_tools_replace_a_tool_in_code():
    from retail_kyc_onboarding.workflow.components import tools

    ns = run_python(blocks(TOOLS_DOC, "Replace a tool in code", "python")[0], TOOLS={})
    assert ns["TOOLS"][REGISTRY_ID].config["timeout"] == 10
    assert ns["TOOLS"][REGISTRY_ID].config["url"] == tools.RETAIL_KYC_ID_REGISTRY_LOOKUP.config["url"]


def test_tools_the_error_shown_is_the_real_one(tmp_path):
    shown = re.search(r"`(content\.tools\.retail_kyc_lookup: unknown tool id\. Known: [^`]*)`", section(TOOLS_DOC, "Check it")).group(1)
    snippet = yaml.safe_dump({"provider-profile": {"default": {"content": {"tools": {"retail_kyc_lookup": {"version": "x"}}}}}})
    with pytest.raises(ConfigError) as error:
        tools_profile(tmp_path, snippet)
    assert shown in str(error.value)
