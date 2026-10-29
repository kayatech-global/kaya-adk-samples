# Configuration

One file, `retail_kyc_onboarding/settings.py`, decides how the agent runs; the exported workflow in `workflow/` is never edited.
No `FLOW_` setting is required: with none set the agent runs in development mode with memory sessions and the default providers (a model step still needs its key, `GOOGLE_API_KEY`).

| In `settings.py` | Purpose |
|----------------|---------|
| `Settings` | The `FLOW_` settings below, read from the environment and from `retail_kyc_onboarding/.env`. |
| `build_providers(settings)` | The providers to run with (identity, secrets, models, tools, sessions, telemetry): the profile of `runtime.yaml`, then your code. |
| `MODELS`, `PROMPTS`, `TOOLS` | Replace one exported model, prompt or tool by id. |
| `build_config(...)` | Combines the three for one build; `agent.py` calls it, for the ADK tools and for `server.py`. |

Precedence, highest first: arguments to `agent.build(...)`, then the maps in `settings.py`, then `content:` in `runtime.yaml`, then the exported workflow.
For providers: code in `build_providers(...)` after the profile is loaded, then the selected profile of `runtime.yaml`, then the built-in defaults ([configure providers](../how-to/configure-providers.md)).

## Settings

`Settings` reads `FLOW_` environment variables and `retail_kyc_onboarding/.env`, the same file `adk run` and `adk web` load for this agent.
An environment variable wins over the file.
Every setting is optional.

| Variable | Default | Meaning |
|----------|---------|---------|
| `FLOW_PROFILE` | `development` | `development` or `production`. Production refuses the anonymous `authn` provider's callers and keeps `/readyz` at 503 while `authn` or `authz` is unhealthy, makes the guard fail closed, requires named model secrets and refuses `FLOW_PRINCIPAL_FROM=fixed`. |
| `FLOW_CONDITIONS_MODE` | `strict` | `strict` (restricted expression walker) or `compat` (reproduces the source system's expression rules). |
| `FLOW_FAIL_CLOSED` | unset | `true` or `false`: whether guard checks fail closed. Unset: closed unless the profile is `development`. |
| `FLOW_SESSION_URI` | `memory://` | Where the run host keeps runs, pauses and run records: `memory://`, `sqlite+aiosqlite:///runs.db` or `postgresql+asyncpg://...`. ADK's `adk web` and `adk api_server` take the same URI as `--session_service_uri`. |
| `FLOW_DEFAULT_MODEL` | empty | A `provider/name` model for every step that named no model of its own. Empty keeps the export's default. See [change the model](../how-to/change-the-model.md). |
| `FLOW_PROVIDER_MODE` | empty | The profile of `runtime.yaml` to run with: `default`, `arc` or one you add. Empty takes the file's `mode` (`default` as shipped). See [configure providers](../how-to/configure-providers.md). |
| `FLOW_RUNTIME_FILE` | `runtime.yaml` | The provider profiles file, relative to the package folder, or an absolute path. A missing file means the built-in defaults. |
| `FLOW_PRINCIPAL_FROM` | `fixed` | Who a run-host agent run is for: `fixed`, `state` (`session.state['flow_principal']`) or `user_id` (the ADK session's user). |
| `FLOW_FIXED_SUBJECT` | `adk-agent` | The subject of runs when `FLOW_PRINCIPAL_FROM` is `fixed`. |
| `FLOW_ALLOW_USER_ID` | `false` | Allow `FLOW_PRINCIPAL_FROM=user_id` in production, when the embedding platform vouches for the user. |

## Other environment variables

- ADK reads its own variables, for example `GOOGLE_API_KEY` and `GOOGLE_GENAI_USE_ENTERPRISE`.
- The Gemini models run on ADK's own Gemini class, which reads `GOOGLE_API_KEY` (or Vertex AI credentials).
- The default `litellm` model provider reads the key its model provider expects, for example `OPENAI_API_KEY` for the `gpt-5.5` fallback.
- A model that names a secret reads it through the `secret` provider; under `FLOW_PROFILE=development` an unset secret falls back to the provider's own variable, under `production` it stops start-up.
- The default `env` secret provider reads one variable per secret, the secret's name in upper case, then the name as written ([bind secrets](../how-to/bind-secrets.md)).

`adk run`, `adk web` and `adk api_server` load all of them from `retail_kyc_onboarding/.env`; so do the FastAPI server and the run host (`settings.build_config`).
A variable already set in the environment wins.

## Providers and replacements

```python
def build_providers(settings: Settings) -> Providers:
    from ._engine.factories.security import JwtOidcAuthn

    p = profile_providers(settings, content=WORKFLOW)
    p.authn = JwtOidcAuthn({"jwks_url": "https://idp.example/jwks", "issuer": "https://idp.example", "audience": "flows"})
    return p


PROMPTS["kyc_intake_agent"] = "New instruction text"
```

`profile_providers(settings, content=WORKFLOW)` returns the providers of the `runtime.yaml` profile (new instances, one typed field per factory kind); see [factories](factories.md) and [EXTENDING.md](../EXTENDING.md).
An id in `MODELS`, `PROMPTS` or `TOOLS` that the export does not have is a mypy error, and at start-up a `ConfigError` that lists the known ids.

## Variables of the workflow

| Name | Type | Default |
|------|------|---------|
| `kyc_risk_agent.summary` | string |  |
| `kyc_intake_agent.full_name` | string |  |
| `kyc_intake_agent.id_number` | string |  |
| `kyc_intake_agent.product` | string |  |
| `kyc_intake_agent.email` | string |  |
| `kyc_risk_agent.kyc_risk` | string |  |
| `kyc_risk_agent.recommendation` | string |  |
| `kyc_risk_agent.reasons` | array |  |
| `id_registry_lookup.registered_name` | string |  |
| `id_registry_lookup.registry_id_number` | string |  |
| `id_registry_lookup.dob` | string |  |
| `id_registry_lookup.address` | string |  |
| `id_registry_lookup.id_status` | string |  |
| `id_registry_lookup.nationality_code` | string |  |
| `id_registry_lookup.pep_flag` | string |  |
| `id_registry_lookup.sanctions_hit` | string |  |
| `account_creation.account_number` | string |  |
| `account_creation.created_customer_id_number` | string |  |
| `account_creation.account_status` | string |  |
