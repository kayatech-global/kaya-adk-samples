# Change the model

This package works with any model LiteLLM supports, and with any ready ADK model (`BaseLlm`).

## Models in this export

| Model entity | Provider | Model name | Constant |
|--------------|----------|------------|----------|
| `gemini_3_8_flash` | google | gemini-3.8-flash | `retail_kyc_onboarding/workflow/components/models.py: GEMINI_3_8_FLASH` |
| `gemini_3_5_flash` | google | gemini-3.5-flash | `retail_kyc_onboarding/workflow/components/models.py: GEMINI_3_5_FLASH` |
| `gpt_5_5` | openai | gpt-5.5 | `retail_kyc_onboarding/workflow/components/models.py: GPT_5_5` |

Every model step names `gemini_3_8_flash`; the other two are its fallbacks.

## Fallbacks

`gemini_3_8_flash` lists `fallbacks=("gemini_3_5_flash", "gpt_5_5")`.
The engine turns that into ADK's own `FallbackModel`: a call that fails with HTTP 429, 500, 502, 503 or 504 (rate limit, overload, outage) is sent to the next model, once each, in order.
Before that, a brief 500, 502 or 503 is tried up to three times in all (two retries) on the same Gemini model, and every model call has a five-minute deadline.
Any other error, and any failure after a model has started answering, reaches the step as it is.
A fallback whose key is not set is left out when the agent starts, so `gpt_5_5` takes part only with `OPENAI_API_KEY` or `MODEL_GPT_5_5_API_KEY` (under `FLOW_PROFILE=production` only `MODEL_GPT_5_5_API_KEY`).
Each fallback logs `Model ... failed with status ...; falling back to the next model.` at warning level.
A replacement in `MODELS` (below) has no fallbacks unless its `ModelSpec` names some.

## Add or remove a fallback

Replace the model with a copy that names other fallbacks (model ids, or `provider/name`):

```python
from dataclasses import replace

from .workflow.components.models import GEMINI_3_8_FLASH

MODELS["gemini_3_8_flash"] = replace(GEMINI_3_8_FLASH, fallbacks=("gpt_5_5",))   # skip Gemini 3.5 Flash
MODELS["gemini_3_8_flash"] = replace(GEMINI_3_8_FLASH, fallbacks=())             # no fallback at all
```

The fallbacks are tried in order; one without a key is left out at start-up.

## Give a model its own API key

Gemini models share one key, `GOOGLE_API_KEY`, which ADK's own Gemini class reads.
Any model can instead name a secret of its own: set `api_key_secret` on its spec, and give the secret a value in your secret store (with the default `env` provider the variable is the secret's name in upper case):

```python
MODELS["gemini_3_8_flash"] = replace(GEMINI_3_8_FLASH, api_key_secret="model_gemini_3_8_flash_api_key")
```

```bash
export MODEL_GEMINI_3_8_FLASH_API_KEY=...      # the key of this model only
```

With a key named and found, the model runs through LiteLLM (`gemini/gemini-3.8-flash`) with that key, instead of ADK's Gemini class; the fallbacks still apply.
ADK warns once about this route (`[GEMINI_VIA_LITELLM]`: it prefers its own Gemini class); set `ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS=true` to silence it.
Without a value in development the shared `GOOGLE_API_KEY` is used as before, and in production the missing secret stops the start-up.
`gpt_5_5` already works this way: it names `model_gpt_5_5_api_key`, or reads `OPENAI_API_KEY`.

## Three ways to change it

1. **Only the default.** Set `FLOW_DEFAULT_MODEL` (for example `openai/gpt-4o`).
   It replaces the model of every step that had none configured and leaves steps with their own model alone.
   Every step of this export names `gemini_3_8_flash`, so here it changes nothing.
2. **One model.** Replace it by id in `MODELS` in `retail_kyc_onboarding/settings.py`, with a `ModelSpec` or a ready ADK model (see below).
3. **Everything.** Choose another `model` provider in `runtime.yaml` (see [configure providers](configure-providers.md)), or return one from `build_providers(...)` in `settings.py`.

## Replace one model

```python
from google.adk.models.lite_llm import LiteLlm

from ._engine.content import ModelSpec

MODELS["gemini_3_8_flash"] = ModelSpec(id="gemini_3_8_flash", provider="google", model_name="gemini-3.1-pro-preview")
# or a ready ADK model, used as it is:
MODELS["gemini_3_8_flash"] = LiteLlm(model="anthropic/claude-sonnet-5-5")
```

A `ModelSpec` is built by the `model` provider; a `BaseLlm` is used as it is.
The id is typed (`ModelId`), so a misspelt id is a mypy error, and at start-up a `ConfigError` that lists the known ids.
The trace reports the replacement as `model:gemini_3_8_flash (override)`.
For one use in code, pass the same mapping to `agent.build(models={...})`, which returns a new `(root_agent, app)` and wins over `settings.py`.

## Model strings and keys

Use LiteLLM's `provider/name` form.
The provider reads its own key from the environment, or from a secret named in the model entity.

| Provider | `FLOW_DEFAULT_MODEL` example | Key variable |
|----------|------------------------------|--------------|
| Gemini (Google AI Studio) | `gemini/gemini-3.5-flash` | `GOOGLE_API_KEY` (ADK's own Gemini class) or `GEMINI_API_KEY` (LiteLLM) |
| Gemini on Vertex AI | `vertex_ai/gemini-3.5-flash` | Application Default Credentials, `VERTEXAI_PROJECT`, `VERTEXAI_LOCATION` |
| OpenAI | `openai/gpt-4o` | `OPENAI_API_KEY` |
| Azure OpenAI | `azure/<deployment>` | `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION` |
| Amazon Bedrock | `bedrock/<model id>` | AWS credentials |
| Anthropic | `anthropic/claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| An internal gateway | the `gateway_openai` provider (below) | the logical secret named in its settings |

The default `litellm` provider receives `FLOW_DEFAULT_MODEL` from the engine.
A `gemini/` name with no key, endpoint, header or extra option runs on ADK's own Gemini class, which reads `GOOGLE_API_KEY` (temperature and max tokens are passed to it through the agent).
If you return another model provider, pass the default yourself, for example `GatewayOpenAIModels({"base_url": ..., "default_model": settings.default_model})`.
With `GatewayOpenAIModels` the value is the gateway's own model name and is sent as written.
With `VertexGeminiModels` give a Gemini model name, with or without the `gemini/` prefix.

## Use a gateway

Point the `model` kind at an OpenAI-compatible gateway in `runtime.yaml`:

```yaml
provider-profile:
  default:
    factories:
      model:
        provider: gateway_openai
        base_url: https://llm-gateway.example.internal/v1
        auth_secret: gateway_token
        model_map: {gemini_3_8_flash: gemini-3-8-flash-prod, gemini_3_5_flash: gemini-3-5-flash-prod}
```

`gateway_token` is a logical secret name, read through the `secret` provider.
`model_map` turns an entity id (or model name) into the name the gateway expects; a model it does not list is sent under its exported name.
The fallback chain keeps working: each model of it is built by the gateway.

## Notes

- The Gemini models leave `temperature` unset: Google recommends the default of 1.0 for Gemini 3 models, and lower values can make them loop.
  The plan's `gpt-5.5` keeps its 0.5, which LiteLLM drops if the model rejects it.
- ADK marks `FallbackModel` experimental and announces it with a warning that its `ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS` switch does not cover; the engine filters that one warning where it builds the model.
