"""model: an ADK model object for a model spec. Specs come from the export's content through ``resolve_model``."""

from __future__ import annotations

import contextvars
import logging
import os
from abc import abstractmethod
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.genai import types

from ..datatypes import ANONYMOUS, ModelSpec, Principal
from ..errors import ConfigError
from .base import Provider, ProviderSettings
from .registry import register

_log = logging.getLogger(__name__)
MODEL_TIMEOUT_SECONDS = 300  # per model call; past it Gemini answers 504 and the call moves on to the model's fallback
# LiteLLM logs every model call at INFO through its own handler and again through the root logger; keep its warnings
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# The principal of the run in progress, so a gateway can fetch per-call credentials (token exchange).
CURRENT_PRINCIPAL: contextvars.ContextVar[Principal] = contextvars.ContextVar("flow_principal", default=ANONYMOUS)

PROVIDER_PREFIX = {
    "openai": "openai",
    "azure": "azure",
    "azure_openai": "azure",
    "anthropic": "anthropic",
    "bedrock": "bedrock",
    "aws": "bedrock",
    "google": "gemini",
    "gemini": "gemini",
    "vertex": "vertex_ai",
    "vertex_ai": "vertex_ai",
    "ollama": "ollama",
    "mistral": "mistral",
    "groq": "groq",
    "cohere": "cohere",
}

# The variables a LiteLLM prefix reads its key from, for telling whether a fallback model can run; an unlisted prefix is assumed ready
KEY_VARIABLES = {
    "openai": ("OPENAI_API_KEY",),
    "azure": ("AZURE_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
}


class ModelFactory(Provider):
    """Factory kind that turns a ModelSpec into an ADK model object."""

    kind = "model"

    @abstractmethod
    async def create(self, spec: ModelSpec) -> BaseLlm:
        """Return the ADK model for ``spec``; async because building it may need a secret."""

    async def ready(self, spec: ModelSpec) -> bool:
        """Whether ``spec`` has the credentials it needs; a fallback model that is not ready is left out."""
        return True


def litellm_name(spec: ModelSpec, provider_map: dict[str, str] | None = None) -> str:
    """LiteLLM model string: keep an explicit 'prefix/name', otherwise derive the prefix from the provider label."""
    name = spec.model_name or spec.id
    if "/" in name:
        return name
    prefix = (provider_map or {}).get(spec.provider.lower()) or PROVIDER_PREFIX.get(spec.provider.lower(), spec.provider.lower())
    return f"{prefix}/{name}" if prefix else name


def resolve_model(unit: Any, ref: str) -> ModelSpec | BaseLlm:
    """The model a node's reference names in a content unit: a client's ready ADK model, the unit's model entity (with any
    override applied), or a plain ``provider/name``.

    Raises:
        ConfigError: The reference is none of those.
    """
    if ref in unit.llms:
        return unit.llms[ref]
    if ref in unit.models:
        return unit.models[ref]
    if "/" in ref:
        return ModelSpec(id=ref, model_name=ref)
    raise ConfigError(f"model '{ref}' is not a model of this export; known: {sorted([*unit.models, *unit.llms])}")


@register
class MockModels(ModelFactory):
    """Test provider: every spec gets the same scripted MockLlm."""

    name = "mock"

    class Settings(ProviderSettings):
        script: list[dict[str, Any]] = []
        default: str = "ok"
        default_model: str = ""  # accepted so the settings of a real provider fit; a mock has no model to replace

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        from ..testing.mock_llm import MockLlm

        self._llm = MockLlm(script=list(self.settings.script), default=self.settings.default)

    @property
    def llm(self) -> Any:
        """The shared MockLlm, for scripting it and inspecting its calls in tests."""
        return self._llm

    async def create(self, spec: ModelSpec) -> BaseLlm:
        """Return the shared MockLlm whatever ``spec`` says."""
        return self._llm


@register
class LiteLlmModels(ModelFactory):
    """Any provider LiteLLM supports. Key from the secret factory when the spec names one, else LiteLLM's env.

    Settings a model does not accept (for example ``temperature`` on a reasoning model) are dropped instead of failing the call.
    Under the development profile a named secret that is not set also falls back to LiteLLM's env (``OPENAI_API_KEY``, ...), so the
    agent starts without it and only a step that calls the model fails. The production profile requires the named secret at start-up.
    """

    name = "litellm"

    class Settings(ProviderSettings):
        provider_map: dict[str, str] = {}
        base_url: str | None = None
        headers: dict[str, str] = {}
        default_model: str = ""  # a "provider/name" that replaces the model of every step that named none (see ModelSpec.default)

    async def ready(self, spec: ModelSpec) -> bool:
        """A key from the named secret or, outside production, the provider's own variable (as ``create`` takes it); a gateway or a
        Vertex AI set-up counts as ready."""
        if spec.base_url or self.settings.base_url or spec.headers or self.settings.headers:
            return True
        if spec.api_key_secret:
            if await self.ctx.provider("secret").get_optional(spec.api_key_secret):
                return True
            if self.ctx.profile == "production":
                return False
        prefix = litellm_name(spec, self.settings.provider_map).split("/", 1)[0]
        if prefix == "gemini" and _vertex_ai():
            return True
        if prefix not in KEY_VARIABLES:
            return True
        return any(os.environ.get(name) for name in KEY_VARIABLES[prefix])

    async def create(self, spec: ModelSpec) -> BaseLlm:
        """Build a LiteLlm for ``spec``, or ADK's Gemini class for a ``gemini/`` name with no key, endpoint, header or extra option."""
        from google.adk.models.lite_llm import LiteLlm

        kwargs: dict[str, Any] = {}
        if spec.api_key_secret:
            secrets = self.ctx.provider("secret")
            strict = self.ctx.profile == "production"
            key = await (secrets.get(spec.api_key_secret) if strict else secrets.get_optional(spec.api_key_secret))
            if key:
                kwargs["api_key"] = key
            else:
                # the documented development set-up (OPENAI_API_KEY and the like); production fails above instead
                _log.info("secret '%s' of model '%s' is not set; the provider's own variables apply", spec.api_key_secret, spec.id)
        base = spec.base_url or self.settings.base_url
        if base:
            kwargs["api_base"] = base
        headers = {**self.settings.headers, **dict(spec.headers)}
        if headers:
            kwargs["extra_headers"] = headers
        kwargs.update(dict(spec.extra))
        sampling = {k: v for k, v in (("temperature", spec.temperature), ("max_tokens", spec.max_tokens)) if v is not None}
        name = (
            self.settings.default_model if spec.default and self.settings.default_model else litellm_name(spec, self.settings.provider_map)
        )
        if name.startswith("gemini/") and not kwargs:
            # ADK's own Gemini class (GOOGLE_API_KEY or Vertex AI) is the recommended path; sampling settings reach it through the
            # agent's generate_content_config, which the adapter builds from the same spec
            return TimedGemini(model=name.removeprefix("gemini/"), retry_options=gemini_retry())
        # a setting the target model rejects (temperature on a reasoning model) is dropped, as the source platform's client does
        return LiteLlm(model=name, **{"drop_params": True, "timeout": MODEL_TIMEOUT_SECONDS, **sampling, **kwargs})


@register
class VertexGeminiModels(ModelFactory):
    """Provider that uses ADK's own Gemini class; credentials come from the environment."""

    name = "vertex_gemini"

    class Settings(ProviderSettings):
        default_model: str = ""  # a Gemini model name that replaces the model of every step that named none (see ModelSpec.default)

    async def create(self, spec: ModelSpec) -> BaseLlm:
        """Return a Gemini for the spec's model name, falling back to its id. A step without a model of its own gets ``default_model``."""
        if spec.default and self.settings.default_model:
            return TimedGemini(model=self.settings.default_model.removeprefix("gemini/"), retry_options=gemini_retry())
        return TimedGemini(model=spec.model_name or spec.id, retry_options=gemini_retry())


class TimedGemini(Gemini):
    """ADK's Gemini class with a deadline on every call: ADK sets none, so a stalled model held a step for over ten minutes."""

    async def generate_content_async(self, llm_request, stream=False):  # type: ignore[no-untyped-def]
        config = llm_request.config
        if config is not None and (config.http_options is None or config.http_options.timeout is None):
            options = config.http_options or types.HttpOptions()
            config.http_options = options.model_copy(update={"timeout": MODEL_TIMEOUT_SECONDS * 1000})
        async for response in super().generate_content_async(llm_request, stream):
            yield response


def gemini_retry() -> Any:
    """Retries for ADK's Gemini class: a brief overload (500, 502, 503) is tried again on the same model; a rate limit (429) or a
    deadline (504) is not, so a ``FallbackModel`` moves on to its next model at once."""
    return types.HttpRetryOptions(attempts=3, initial_delay=1, max_delay=8, http_status_codes=[500, 502, 503])


def _vertex_ai() -> bool:
    """Whether ADK's Gemini class is set to Vertex AI, which authenticates with Google credentials instead of an API key."""
    flags = (os.environ.get(name, "") for name in ("GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GENAI_USE_VERTEXAI"))
    return any(flag.strip().lower() in {"1", "true", "yes"} for flag in flags)
