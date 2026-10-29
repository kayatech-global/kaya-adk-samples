# Extending this agent

Everything you change lives in `runtime.yaml` and `settings.py`: the providers the workflow runs with, and replacements for single models, prompts and tools.
The exported workflow (`workflow/`) and the vendored engine (`_engine/`, version 0.1.0) are not edited; you can read and debug the engine, but a change there is lost on the next export.

| Kind | You provide | Base class |
|------|-------------|------------|
| prompt | where prompts come from (default `content`: this export's prompts with your replacements applied) | `retail_kyc_onboarding._engine.factories.prompt.PromptFactory` |
| model | a model or your model gateway | `retail_kyc_onboarding._engine.factories.gateway.GatewayAdapter` or `...model.ModelFactory` |
| secret | your vault | `retail_kyc_onboarding._engine.factories.secret.SecretFactory` |
| api, mcp | how tools are called | `...factories.api.ApiFactory`, `...mcp.McpFactory` |
| session | where run state lives | `...factories.session.SessionFactory` |
| telemetry | trace and audit sinks | `...factories.telemetry.TelemetryFactory` |
| adk_plugin | extra Google ADK plugins (policy, guardrail, audit, Model Armor) | `...factories.adk_plugin.AdkPluginFactory`; see below |
| authn, authz, credentials | identity, entitlements, outbound credentials | `...factories.security.*` |
| approval_policy, approver_resolver, approval_channel | tool approval | `...factories.approval.*` |
| guardrail, retrieval, registration, runstore | checks, knowledge, registry, run records | `...factories.misc.*` |
| router | how a branch is chosen | `...factories.router.RouterStrategy` |

## Choose the providers

Two ways, and they combine.

**In `runtime.yaml`, by name.**
A mode lists the provider of each kind and its settings; `FLOW_PROVIDER_MODE` picks the mode.
A provider that is not built in is named by its class path:

```yaml
provider-profile:
  default:
    factories:
      secret: {provider: retail_kyc_onboarding.my_providers:VaultSecrets, url: "https://vault.example.internal"}
```

How the file works, with examples: [configure providers](how-to/configure-providers.md).
The optional `arc` mode and its empty placeholder providers: [Extending for ARC](../README.md#extensibility---support-for-enterprise-frameworks-such-as-arc).

**In code.**
`build_providers(settings)` in `settings.py` returns the providers to run with.
It starts from the profile of `runtime.yaml` (the defaults, as shipped), a `Providers` dataclass with one typed field per kind; change the fields you need and return it.
Code wins over the file:

```python
def build_providers(settings: Settings) -> Providers:
    from ._engine.factories.api import HttpApiFactory
    from ._engine.factories.security import JwtOidcAuthn
    from .my_providers import VaultSecrets

    p = profile_providers(settings, content=WORKFLOW)
    p.authn = JwtOidcAuthn({"jwks_url": "https://idp.example/jwks", "issuer": "https://idp.example", "audience": "flows"})
    p.secret = VaultSecrets({"url": "https://vault.example.internal"})
    p.api = HttpApiFactory(HttpApiFactory.Settings(allow_hosts=["608b7a57737e470017b74d29.mockapi.io", "api.example.internal"]))
    return p
```

Settings are typed: pass the provider's own `Settings` or a dict; an unknown key is an error that names the provider.
A provider instance serves one engine, so build new instances inside `build_providers(...)`, never at module level.
`settings` holds the `FLOW_` variables, so a provider can depend on them.

## Write a provider

Put your classes in a module of your own inside the agent folder, for example `retail_kyc_onboarding/my_providers.py`:

```python
import httpx

from ._engine.factories.base import ProviderSettings
from ._engine.factories.secret import SecretFactory, SecretNotFound


class VaultSecrets(SecretFactory):
    """Reads each secret from a vault service over HTTP."""

    name = "vault"

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

A provider sets `kind` (inherited from its base class) and `name`, may declare `Settings` (unknown keys are rejected), and implements the base class methods.
Check it with the contract kits in `retail_kyc_onboarding._engine.testing.contract` (secret, authn, authz, credentials, prompt, router and adk_plugin); test other kinds directly.
Import the engine only as `retail_kyc_onboarding._engine` (relatively, `._engine`): it is a private copy, which is what lets another agent run in the same process without sharing modules with this one.

## Replace single entities

The maps in `settings.py` replace one model, prompt or tool by id:

```python
from google.adk.models.lite_llm import LiteLlm

MODELS["gemini_3_8_flash"] = LiteLlm(model="anthropic/claude-sonnet-5-5")  # a ModelSpec or a ready ADK BaseLlm
PROMPTS["kyc_intake_agent"] = "New instruction text"  # new text or a whole Prompt
```

`TOOLS` takes a `ToolDef` or a ready `ToolHandle`; every call still passes the guard.
The ids are typed (`ModelId`, `PromptId`, `ToolId`), so a misspelt id is a mypy error, and at start-up a `ConfigError` that lists the known ids.
Each replacement shows in the trace as `<kind>:<id> (override)`.
Precedence, highest first: arguments to `agent.build(...)`, then the maps in `settings.py`, then `content:` in `runtime.yaml`, then the exported workflow.

## Add an ADK plugin

An ADK plugin sees every agent, model call and tool call of a run:

```python
from google.adk.plugins.base_plugin import BasePlugin


class AuditPlugin(BasePlugin):
    def __init__(self, name: str = "audit"):
        super().__init__(name=name)

    async def before_model_callback(self, *, callback_context, llm_request):
        ...  # inspect or log; return None to continue, or an LlmResponse to answer instead of the model
        return None
```

Name its class in `runtime.yaml`; each class is built with no arguments:

```yaml
provider-profile:
  default:
    factories:
      adk_plugin: {provider: static, plugins: ["retail_kyc_onboarding.my_providers:AuditPlugin"]}
```

Or hand instances to the engine in `build_providers(...)`:

```python
    from ._engine.factories.adk_plugin import StaticAdkPlugins
    from .my_providers import AuditPlugin

    p.adk_plugin = StaticAdkPlugins([AuditPlugin()])
```

Under the run host the engine's trace plugin runs first; under `adk run` and `adk web` only the plugins you list run.
Plugin names must be unique; a plugin that is not an ADK `BasePlugin` stops the start-up with a message that names it.

## Versions

Every prompt, model and tool is a constant with its own `version` attribute ([tools](reference/tools.md)).
To run a different version of one part, replace it in `settings.py`, or export the version you want again from Kaya.
