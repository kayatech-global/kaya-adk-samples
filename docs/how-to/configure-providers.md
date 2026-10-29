# Configure providers in runtime.yaml

`runtime.yaml`, next to `settings.py`, says which provider serves each factory kind (secrets, models, tools, identity, sessions, telemetry, ...), per mode.
Two modes ship: `default` (what the agent runs with) and the optional `arc`, which selects the empty placeholder providers of `arc_providers.py` ([Extending for ARC](../../README.md#extensibility---support-for-enterprise-frameworks-such-as-arc)).
You change a provider by editing the file, not the code; the code in `settings.py` still wins when you need it.

Every worked example below is a test (`tests/unit/test_doc_examples.py` runs it), so they work as written.

## The file

```yaml
mode: ${env:FLOW_PROVIDER_MODE:-default}   # which profile runs

provider-profile:
  default:
    factories:
      secret: env                           # a provider name
      session: uri
      telemetry: console
      # ... the other kinds (any kind left out keeps its built-in default)
  arc:
    config: {}                              # given to every provider named `arc`
    factories:
      all: arc                              # only what differs from `default`: here every kind with an `arc` provider
```

- `mode` picks the profile. `FLOW_PROVIDER_MODE` wins over it.
- A profile lists only the kinds it changes; every other kind comes from `default` (or from the profile named in `extends`).
- A value is a provider name, or `{provider: <name>, <setting>: <value>}` to give the provider settings.
- `${env:NAME}` and `${env:NAME:-fallback}` read the environment, and the `.env` beside `settings.py`, when the profile is selected.
  A mode you do not select is checked but its variables are never resolved, so its missing variables never stop another mode.
- `all: arc` makes every kind that has an `arc` provider use it; an explicit line for a kind in the same profile, or in a profile that extends it, wins over it, while explicit lines in a parent profile (such as `default`) do not.
- The kinds are the factory kinds of [factories](../reference/factories.md).
  Any other key (for example `execution`) is an error that lists the valid ones.

## Switch the mode

```bash
FLOW_PROVIDER_MODE=staging adk run retail_kyc_onboarding
```

`staging` is a mode you add ([below](#add-your-own-mode)); `arc` runs once its placeholder providers are implemented.

The first trace event, `engine.started`, reports `mode` and the provider of each kind.
Unset the variable and the agent is back in `default` mode.

## Change one provider

Keep the shipped profile and change only what you need.
This keeps runs in a SQLite file, writes the trace to a file and checks callers' tokens against your identity provider:

```yaml
provider-profile:
  default:
    factories:
      session: {provider: uri, uri: "sqlite+aiosqlite:///runs.db"}
      runstore: {provider: uri, uri: "sqlite+aiosqlite:///runs.db"}
      telemetry: {provider: jsonl, path: runs/trace.jsonl}
      authn:
        provider: jwt_oidc
        jwks_url: https://idp.example/jwks
        issuer: https://idp.example
        audience: kyc-onboarding
```

Edit the `default` profile in `runtime.yaml` in place (keep the other lines) and run the agent as usual.
Check it: the `engine.started` event lists `session uri`, `telemetry jsonl` and `authn jwt_oidc`.
Which providers exist for each kind, and their settings: [factories](../reference/factories.md).
With `FLOW_SESSION_URI` set, a `uri` provider named alone (`session: uri`) takes it; once you give the provider settings, write `uri` yourself, for example `uri: ${env:FLOW_SESSION_URI:-memory://}`.

## Give a provider settings

Settings go beside `provider`.
A provider checks them when the agent starts, and an unknown key stops it with the provider's name:

```yaml
provider-profile:
  default:
    factories:
      telemetry: {provider: jsonl, pathh: runs/trace.jsonl}
```

```text
Invalid settings for factory 'telemetry' provider 'jsonl': pathh: Extra inputs are not permitted
```

Fix the key (`path`).
The settings of a provider are its `Settings` class in `_engine/factories/`.

## Use a provider of your own

Write the class ([EXTENDING.md](../EXTENDING.md#write-a-provider)) in a module of the agent folder, for example `retail_kyc_onboarding/my_providers.py`, and name it by its class path:

```python
import httpx

from ._engine.factories.base import ProviderSettings
from ._engine.factories.secret import SecretFactory, SecretNotFound


class VaultSecrets(SecretFactory):
    name = "my_vault"

    class Settings(ProviderSettings):
        url: str
        token_env: str = "VAULT_TOKEN"  # noqa: S105 - the name of an environment variable

    async def get(self, name: str) -> str:
        token = self.ctx.env.get(self.settings.token_env, "")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{self.settings.url}/secrets/{name}", headers={"X-Vault-Token": token})
        if resp.status_code != 200:
            raise SecretNotFound(name, f"{self.name} (HTTP {resp.status_code})")
        return str(resp.json()["value"])
```

```yaml
provider-profile:
  default:
    factories:
      secret:
        provider: retail_kyc_onboarding.my_providers:VaultSecrets
        url: https://vault.example.internal
```

The class path works wherever a provider name does.
A class of another kind (a telemetry class named under `secret`) is an error that says so.
`retail_kyc_onboarding._engine.testing.contract` has contract kits for the secret, authn, authz, credentials, prompt, router and adk_plugin kinds; run the one for your kind, or test other kinds directly.

## Add your own mode

A mode is a profile.
This one, `staging`, is the default with production-like identity and durable runs; it needs no copy of the rest:

```yaml
provider-profile:
  staging:
    extends: default
    factories:
      authn: {provider: jwt_oidc, jwks_url: "https://idp.staging.example/jwks", issuer: "https://idp.staging.example"}
      session: {provider: uri, uri: "${env:STAGING_SESSION_URI}"}
```

Run it with `FLOW_PROVIDER_MODE=staging STAGING_SESSION_URI=postgresql+asyncpg://... adk run retail_kyc_onboarding`.
`extends` may name any profile; a profile with no `extends` extends `default`.
Profiles that extend each other in a circle are an error.

## Use a different file

`FLOW_RUNTIME_FILE` names another file, relative to the package folder or absolute; without a file the defaults apply.
This tries a local variant, runs in a SQLite file and the trace in a file, without changing the shipped file:

```yaml
mode: local
provider-profile:
  local:
    factories:
      session: {provider: uri, uri: "sqlite+aiosqlite:///runs.db"}
      telemetry: {provider: jsonl, path: runs/trace.jsonl}
```

```bash
FLOW_RUNTIME_FILE=/path/to/runtime.local.yaml adk run retail_kyc_onboarding
```

The other kinds come from the built-in defaults, since this file has no `default` profile.
Keep local files like this out of version control.

## Route or override one entity

`content:` in a profile treats one prompt, model or tool differently from the rest, with no code.
`provider` sends that entity to another provider of its kind; every other key changes a field of the entity:

```yaml
provider-profile:
  staging:
    content:
      prompts:
        kyc_risk_agent_prompt: {provider: content}                  # this prompt always comes from the export, whatever the profile's prompt provider
        kyc_intake_agent: {file: prompts/intake.md, version: v-2}   # or: {text: "..."}
      models:
        gpt_5_5: {provider: litellm}                                 # this model is always called through LiteLLM
        gemini_3_8_flash: {model_name: gemini-3.9-flash, fallbacks: [gpt_5_5], api_key_secret: my_key}
      tools:
        retail_kyc_id_registry_lookup: {provider: http}              # this tool is always called directly over HTTP
        retail_kyc_account_creation: {config: {timeout: 30}}         # a patch of the tool's config
```

- The ids are the entity ids of `workflow/components/`; an unknown id or field is an error that lists the valid ones.
- `provider` is a name or a class path; its settings go beside it in `settings:`, for example `{provider: gateway_openai, settings: {base_url: "https://llm.example.internal/v1"}}`.
  A route is useful when the profile serves the kind from another provider, for example the `arc` placeholders once implemented; a route to `arc` takes the profile's `config` like every `arc` provider.
- Prompt fields: `text`, `file` (relative to `runtime.yaml`), `version`.
  Model fields: the fields of `ModelSpec` (`model_name`, `version`, `default`, `temperature`, `max_tokens`, `fallbacks`, `api_key_secret`, `base_url`, `headers`, `extra`).
  Tool fields: `description`, `config` and `approval` (merged into the exported ones), `manual_execution`, `version` ([update tools](update-tools.md)).
- The trace shows an overridden entity as `<kind>:<id> (override)` and a routed kind as `routed`, listing each route.
- Branch conditions and the graph are not configured here: they are the generated workflow.

## Add a provider class without touching any other file

List the module that defines it under `plugins:`; importing it registers its `@register` classes, and `factories:` or `content:` can name them:

```yaml
plugins: [retail_kyc_onboarding.my_providers]

provider-profile:
  default:
    content:
      prompts:
        kyc_intake_agent: {provider: acme}     # name = "acme" in the class
```

This is how the shipped file brings in the `arc` placeholders: `plugins: [retail_kyc_onboarding.arc_providers]`.

## What wins

From highest to lowest:

1. A provider you set in code in `settings.build_providers(...)` after the profile is loaded.
2. The selected profile of `runtime.yaml` (with its parents).
3. The built-in defaults.

```python
def build_providers(settings: Settings) -> Providers:
    from ._engine.factories.telemetry import OtlpTelemetry

    p = profile_providers(settings, content=WORKFLOW)  # runtime.yaml, as shipped; content= applies its `content:` section
    p.telemetry = OtlpTelemetry({"endpoint": "https://collector.example.internal:4318/v1/traces"})  # code wins
    return p
```

`FLOW_DEFAULT_MODEL` and `FLOW_SESSION_URI` still feed the `litellm` and `uri` providers when a profile names them alone (`model: litellm`, `session: uri`).
Replacing one prompt, model or tool is not a provider change: use `PROMPTS`, `MODELS` and `TOOLS` in `settings.py` ([update prompts](update-prompts.md), [change the model](change-the-model.md)).

## Read the start-up errors

Every error names the file, the profile and the key, and lists the valid choices.

| Message | Fix |
|---------|-----|
| `runtime.yaml: unknown mode 'nope'. Profiles: arc, default` | Pick one of the listed modes or add the profile. |
| `runtime.yaml: provider-profile.default.factories: unknown kind 'execution'. Kinds: secret, credentials, ...` | Use a kind from the list. |
| `No provider 'vaultt' for factory kind 'secret'. Known providers: ...` | Fix the name, or use a class path. |
| `runtime.yaml: provider-profile.staging.config.url: environment variable STAGING_URL is not set (write ${env:STAGING_URL:-<default>} to give it a default)` | Set the variable, or give it a default. |
| `runtime.yaml: provider-profile 'a' extends itself through ...` | Break the `extends` circle. |
| `FLOW_PROVIDER_MODE=arc needs .../runtime.yaml, which does not exist` | Restore the file, or unset the variable. |
| `Invalid settings for factory 'telemetry' provider 'jsonl': ...` | Fix the setting the message names. |
