# Factories and providers

A factory kind is an interface for one external component.
A provider is one implementation.
`runtime.yaml` names, per mode, the provider of each kind (`default` or `arc`); `build_providers(settings)` in `settings.py` loads that profile, and code written there wins over the file.
How to use the file: [configure providers](../how-to/configure-providers.md).

## Modes of this export

`runtime.yaml` ships two profiles; the mode is `FLOW_PROVIDER_MODE` or the file's `mode`.

| Kind | `default` | `arc` |
|------|-----------|-------|
| `secret` | `env` | `arc` |
| `credentials` | `none` | `arc` |
| `authn` | `anonymous` | `arc` |
| `authz` | `allow_all` | `arc` |
| `prompt` | `content` | `arc` |
| `model` | `litellm` | `arc` |
| `api` | `http` | `arc` |
| `mcp` | `mcp_client` | `arc` |

Every other kind is the same in both modes (the table below).
The `arc` providers are optional, empty placeholders in `arc_providers.py`, used only in the `arc` mode ([Extending for ARC](../../README.md#extensibility---support-for-enterprise-frameworks-such-as-arc)).

## Defaults of this export

| Kind | Provider |
|------|----------|
| `adk_plugin` | `none` |
| `api` | `http` |
| `approval_channel` | `adk_request_input` |
| `approval_policy` | `by_tool` |
| `approver_resolver` | `standard` |
| `authn` | `anonymous` |
| `authz` | `allow_all` |
| `credentials` | `none` |
| `guardrail` | `none` |
| `mcp` | `mcp_client` |
| `model` | `litellm` |
| `prompt` | `content` |
| `registration` | `noop` |
| `retrieval` | `none` |
| `router` | `llm_structured` |
| `runstore` | `uri` |
| `secret` | `env` |
| `session` | `uri` |
| `telemetry` | `console` |

## Built-in providers

| Kind | Providers |
|------|-----------|
| `prompt` | `content`, `mock` |
| `model` | `litellm` (any LiteLLM provider), `gateway_openai` (OpenAI-compatible gateway), `vertex_gemini`, `mock` |
| `secret` | `env`, `file`, `google_secret_manager`, `hashicorp_vault`, `mock` |
| `api` | `http`, `mock` |
| `mcp` | `mcp_client`, `mock` |
| `session` | `uri` (follows `FLOW_SESSION_URI`), `memory`, `sqlalchemy` (SQLite or PostgreSQL) |
| `telemetry` | `console`, `jsonl`, `otlp`, `memory` |
| `authn` | `anonymous`, `jwt_oidc`, `keycloak_static_key`, `api_key`, `trusted_header`, `mock` |
| `authz` | `allow_all`, `rules`, `http_pdp`, `mock` |
| `credentials` | `none`, `static_secret`, `oauth_client_credentials`, `token_exchange`, `passthrough` |
| `approval_policy` | `never`, `always`, `by_tool` |
| `approver_resolver` | `standard` |
| `approval_channel` | `adk_request_input`, `http_callback`, `queue`, `auto` |
| `router` | `llm_structured`, `rules` |
| `runstore` | `uri` (follows `FLOW_SESSION_URI`), `session`, `sqlalchemy` |
| `adk_plugin` | `none`, `static` (`{provider: static, plugins: ["pkg.module:Class"]}` in YAML, `StaticAdkPlugins([plugin, ...])` in code) |
| `guardrail` | `none`, `regex`, `http` |
| `retrieval` | `none`, `memory`, `http` |
| `registration` | `noop`, `file`, `http` |

A provider can also be named by a class path, `package.module:Class`, wherever a name is accepted in `runtime.yaml`.
The names are each provider's `name`, as the trace and `/v1/export` report them.
The classes are in `retail_kyc_onboarding._engine.factories`, for example the `hashicorp_vault` secret provider is `factories.secret.HashicorpVaultSecrets`.
Base classes and how to write your own: [EXTENDING.md](../EXTENDING.md).
