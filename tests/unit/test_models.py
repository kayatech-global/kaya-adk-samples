"""Every model step runs on Gemini; ADK's FallbackModel moves on to a backup Gemini, then to gpt-5.5 only when an OpenAI key is set."""

import json

import pytest
from google.adk.models import FallbackModel, Gemini
from google.adk.models.lite_llm import LiteLlm

from retail_kyc_onboarding._engine.app import AppConfig, FlowSettings, Overrides, default_providers
from retail_kyc_onboarding._engine.runtime.entry import build_engine
from retail_kyc_onboarding.workflow import WORKFLOW

KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "MODEL_GPT_5_5_API_KEY")


def model_refs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "model" and isinstance(item, str):
                yield item
            yield from model_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from model_refs(item)


async def chain(monkeypatch, **env):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = FlowSettings()
    engine = await build_engine(WORKFLOW, AppConfig(settings=settings, providers=default_providers(settings), overrides=Overrides()))
    return await engine.model_for(engine.unit, "gemini_3_8_flash")


def test_every_model_step_runs_on_gemini_3_8_flash():
    graph = json.loads(WORKFLOW.graph.model_dump_json())
    assert set(model_refs(graph)) == {"gemini_3_8_flash"}


async def test_gemini_falls_back_to_gemini_then_to_openai_when_its_key_is_set(monkeypatch):
    model = await chain(monkeypatch, GOOGLE_API_KEY="g", OPENAI_API_KEY="o")
    assert isinstance(model, FallbackModel)
    primary, backup, openai = model.models
    assert isinstance(primary, Gemini) and primary.model == "gemini-3.8-flash"
    assert isinstance(backup, Gemini) and backup.model == "gemini-3.5-flash"
    assert isinstance(openai, LiteLlm) and openai.model == "openai/gpt-5.5"
    assert {429, 503} <= model.retriable_status_codes
    assert primary.retry_options.http_status_codes == [500, 502, 503]  # a 429 or a 504 (deadline) moves on to the next model at once


@pytest.mark.parametrize("env", [{"GOOGLE_API_KEY": "g"}, {"GOOGLE_API_KEY": "g", "GOOGLE_GENAI_USE_ENTERPRISE": "FALSE"}])
async def test_openai_is_left_out_without_its_key(monkeypatch, env):
    model = await chain(monkeypatch, **env)
    assert [m.model for m in model.models] == ["gemini-3.8-flash", "gemini-3.5-flash"]


async def test_the_named_openai_secret_also_enables_the_fallback(monkeypatch):
    model = await chain(monkeypatch, GOOGLE_API_KEY="g", MODEL_GPT_5_5_API_KEY="o")
    assert [m.model for m in model.models][-1] == "openai/gpt-5.5"


async def test_production_takes_the_openai_fallback_only_from_its_named_secret(monkeypatch):
    model = await chain(monkeypatch, GOOGLE_API_KEY="g", OPENAI_API_KEY="o", FLOW_PROFILE="production")
    assert [m.model for m in model.models] == ["gemini-3.8-flash", "gemini-3.5-flash"]


async def test_every_gemini_call_carries_the_five_minute_deadline(monkeypatch):
    """ADK's Gemini class sets no deadline; a stalled model held a step for over ten minutes. Past the deadline Gemini answers 504."""
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    from retail_kyc_onboarding._engine.factories.model import (
        MODEL_TIMEOUT_SECONDS,
        TimedGemini,
    )

    seen = []

    async def answer(self, llm_request, stream=False):
        seen.append(llm_request.config.http_options.timeout)
        yield "ok"

    monkeypatch.setattr(Gemini, "generate_content_async", answer)
    request = LlmRequest(config=types.GenerateContentConfig())
    assert [r async for r in TimedGemini(model="gemini-3.8-flash").generate_content_async(request)] == ["ok"]
    assert MODEL_TIMEOUT_SECONDS == 300 and seen == [300_000]  # milliseconds
