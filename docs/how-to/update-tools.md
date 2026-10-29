# Update tools

A tool is an API the workflow calls.
This export has two, in `retail_kyc_onboarding/workflow/components/tools.py` (generated from Kaya; do not edit it):

| Tool id | Constant | Called by the step | Request |
|---------|----------|--------------------|---------|
| `retail_kyc_id_registry_lookup` | `RETAIL_KYC_ID_REGISTRY_LOOKUP` | `look_up_id_registry` | `GET .../customers?id_number=...` |
| `retail_kyc_account_creation` | `RETAIL_KYC_ACCOUNT_CREATION` | `create_active_account` | `POST .../accounts` with a JSON body |

Both steps are tool steps: they run the tool without a model, build the arguments from the run's variables and map the answer back into variables (`request_structures` and `output_structures` in `workflow/graph.py`).

A tool has two parts, and each is changed in its own place:

- **What the tool is**: its URL, method, headers, authorization, timeout, retries and approval.
  The export holds it; you override fields under `content.tools.<id>` in `runtime.yaml`.
- **Who calls it**: the `api` provider.
  `http` calls the URL itself (the default); a class of your own, an `arc` one for example, can do anything else.
  You choose it under `factories:` for every tool, or with `provider` for one tool.

Every example is a test (`tests/unit/test_doc_examples.py`).

## What you can change

| Field under `content.tools.<id>` | Holds |
|----------------------------------|-------|
| `config.url` | the URL; `{name}` in the path is filled from an argument of that name |
| `config.method` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` |
| `config.headers` | a list of `{name, value}` or `{name, secret}` |
| `config.authorization` | how to authenticate ([below](#add-authentication-to-a-tool)) |
| `config.timeout`, `config.retry_attempts` | seconds per attempt, retries after a 5xx or a network error |
| `config.default_query_parameters` | query parameters sent on every call |
| `approval` | whether a call waits for an approver ([below](#require-approval-before-a-tool-runs)); checked by the engine whichever provider calls the tool |
| `description` | what the tool does, shown to a model that calls the tool |
| `version`, `manual_execution` | the tool's version label; whether a person runs it |

The `config` fields are read by the `http` provider; a provider of your own decides which of them it uses.

`config` and `approval` are merged one key at a time into the exported ones: a key you give replaces that key's whole value, so a new `headers` replaces the whole list.
The arguments (`parameters`) and how a step maps them are part of the workflow; change them in Kaya and export again.

## Point a tool at another URL

For example the registry lookup on a staging API, with a shorter timeout and one retry:

```yaml
provider-profile:
  default:
    content:
      tools:
        retail_kyc_id_registry_lookup:
          config:
            url: https://registry.staging.example.internal/api/v1/customers
            timeout: 10
            retry_attempts: 1
```

The method, headers and authorization stay as exported.
To limit which hosts tools may call, give the `http` provider `allow_hosts`; a URL outside the list is refused:

```yaml
provider-profile:
  default:
    factories:
      api: {provider: http, allow_hosts: [registry.staging.example.internal, 608b7a57737e470017b74d29.mockapi.io]}
```

## Add authentication to a tool

Both tools are exported with `authorization: {type: no-auth}`.
To send a bearer token to the account API:

```yaml
provider-profile:
  default:
    content:
      tools:
        retail_kyc_account_creation:
          config:
            url: https://accounts.example.internal/v1/accounts
            authorization: {type: bearer, token_secret: accounts_api_token}
            headers:
              - {name: Content-Type, value: application/json}
              - {name: X-Client-Id, secret: accounts_client_id}
```

The token is never written in the file: `token_secret` and `secret` are logical names, read through the `secret` provider.
With the default `env` provider, export `ACCOUNTS_API_TOKEN` and `ACCOUNTS_CLIENT_ID` ([bind secrets](bind-secrets.md)).

| `type` | Other keys | Sends |
|--------|------------|-------|
| `no-auth` | none | nothing |
| `bearer` | `token_secret`, optional `header_prefix` (default `Bearer`) | `Authorization: Bearer <token>` |
| `basic` | `username` or `username_secret`, and `password_secret` | `Authorization: Basic ...` |
| `api-key` | `key_secret`, optional `header_name` (default `x-api-key`) | `<header_name>: <key>` |
| `oauth2` | `token_url`, `client_id`, `client_secret_secret`, optional `scope`, `audience` | a client-credentials token, cached until shortly before it expires |

Headers from the `credentials` provider are added after these, so a platform can attach its own per call.

## Require approval before a tool runs

```yaml
provider-profile:
  default:
    content:
      tools:
        retail_kyc_account_creation:
          approval:
            policy: always
            approvers: {kind: roles, any_of: [compliance_officer]}
            timeout_seconds: 3600
            on_timeout: deny
```

The call then pauses until an approver with the role answers ([approve and resume](approve-and-resume.md)).
`policy` is `never`, `always`, `by_tool` (with `required`), `by_rule` (with `rules: {when: "<condition>"}`) or `from_authz` (when the `authz` provider answers `require_approval`).
`approvers.kind` is `any_authenticated`, `roles`, `users`, `groups` or `external_system`.
The `approval_policy` provider must be `by_tool`, the default, for a tool's own settings to count.

## Call tools some other way

When tools are reached through something with no provider yet, for example an internal API gateway, write one class in a module of your own, `retail_kyc_onboarding/my_providers.py`:

```python
import httpx

from ._engine.datatypes import CallContext, ToolDef, ToolResult
from ._engine.factories.api import ApiFactory, ToolHandle
from ._engine.factories.base import ProviderSettings
from ._engine.factories.registry import register


class GatewayToolHandle(ToolHandle):
    def __init__(self, factory: "ToolGatewayApi", definition: ToolDef):
        super().__init__(definition)
        self.factory = factory

    async def invoke(self, args: dict, call: CallContext) -> ToolResult:
        token = await self.factory.ctx.provider("secret").get(self.factory.settings.token_secret)
        async with httpx.AsyncClient(timeout=self.factory.settings.timeout) as client:
            resp = await client.post(
                f"{self.factory.settings.url}/tools/{self.name}",
                json=args,
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": call.idempotency_key or ""},
            )
        if resp.status_code >= 400:
            return ToolResult(False, text=resp.text, status=resp.status_code, error=f"HTTP {resp.status_code}")
        return ToolResult(True, text=resp.text, data=resp.json(), status=resp.status_code)


@register
class ToolGatewayApi(ApiFactory):
    """Every tool through an internal gateway: POST {url}/tools/{tool id} with the arguments as JSON."""

    name = "tool_gateway"

    class Settings(ProviderSettings):
        url: str
        token_secret: str = "tool_gateway_token"
        timeout: float = 30

    def build(self, tool: ToolDef) -> ToolHandle:
        return GatewayToolHandle(self, tool)
```

Then name it in `runtime.yaml`, for every tool or for one:

```yaml
plugins: [retail_kyc_onboarding.my_providers]

provider-profile:
  default:
    content:
      tools:
        retail_kyc_account_creation: {provider: tool_gateway, settings: {url: "https://tools.example.internal"}}
```

`data` must be the API's own answer, because the step's `output_structures` read it (the registry's `results[0]`, for example).

## Replace a tool in code

`TOOLS` in `retail_kyc_onboarding/settings.py` does the same as `content.tools` and wins over `runtime.yaml`:

```python
from .workflow.components.tools import RETAIL_KYC_ID_REGISTRY_LOOKUP

TOOLS["retail_kyc_id_registry_lookup"] = RETAIL_KYC_ID_REGISTRY_LOOKUP.with_timeout(10)
```

It takes a `ToolDef` (the exported one changed with `dataclasses.replace`, for example) or a ready `ToolHandle`.
The id is typed (`ToolId`), so a misspelt id is a mypy error, and at start-up a `ConfigError` that lists the known ids.

## Check it

- The start-up trace event `engine.started` lists `tool:<id> (override)` under `modified_entities` for every tool you changed, and the `api` provider of each tool.
- A misspelt tool id or field in `runtime.yaml` stops the start-up with the valid ones, for example `content.tools.retail_kyc_lookup: unknown tool id. Known: retail_kyc_account_creation, retail_kyc_id_registry_lookup`.
- `tests/unit/test_doc_examples.py` runs every example of this page against stubbed APIs.

## Change it in Kaya

For a change that belongs in the workflow itself (new arguments, another mapping, a new tool), edit the tool in Kaya and export again.
A re-export brings it into `workflow/components/tools.py`; keep your `runtime.yaml` and `settings.py` ([re-export from Kaya](../../README.md#re-export-from-kaya)).
