"""The models of plan-retail_kyc_onboarding, one constant each. Generated: point a model elsewhere in settings.py, not here."""

# ruff: noqa: E501, S105, S106
from typing import Final, Literal

from ..._engine.content import ModelSpec

# Every step runs on Gemini through ADK's own Gemini class (GOOGLE_API_KEY). When it is overloaded or down (HTTP 429 or 5xx)
# the call moves on to the next fallback; gpt-5.5 takes part only when an OpenAI key is set.
GEMINI_3_8_FLASH: Final = ModelSpec(
    id="gemini_3_8_flash",
    version="v-gemini-3-8-flash",
    provider="google",
    model_name="gemini-3.8-flash",
    fallbacks=("gemini_3_5_flash", "gpt_5_5"),
    sha256="sha256:af868967f5cf9518f370d8625bbd2eaba4ad9abf55709e07f1c0688d295ea8d6",
)

GEMINI_3_5_FLASH: Final = ModelSpec(
    id="gemini_3_5_flash",
    version="v-gemini-3-5-flash",
    provider="google",
    model_name="gemini-3.5-flash",
    sha256="sha256:1f6814a4cb31af09996e8da7da4ade4f04a86062ecbfcfa3c16fef0a642de901",
)

GPT_5_5: Final = ModelSpec(
    id="gpt_5_5",
    version="v-261b6b2b",
    provider="openai",
    model_name="gpt-5.5",
    temperature=0.5,
    api_key_secret="model_gpt_5_5_api_key",
    sha256="sha256:22ad0c557bf023f3f40dc90d93fc6328cd0149beee839733ca7558d58013add3",
)

ModelId = Literal["gemini_3_8_flash", "gemini_3_5_flash", "gpt_5_5"]
MODELS: Final[dict[str, ModelSpec]] = {
    "gemini_3_8_flash": GEMINI_3_8_FLASH,
    "gemini_3_5_flash": GEMINI_3_5_FLASH,
    "gpt_5_5": GPT_5_5,
}
