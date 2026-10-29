# Bind secrets

The export never contains a secret value.
It refers to each secret by a logical name; `SECRETS` in `retail_kyc_onboarding/workflow/__init__.py` lists the names it needs and which part uses each.

| Name | Kind | Used by | Environment variable (default `env` provider) |
|------|------|---------|-----------------------------------------------|
| `model_gpt_5_5_api_key` | api_key | model:gpt-5.5 | `MODEL_GPT_5_5_API_KEY` |

## With the default `env` provider

The `env` secret provider reads the environment variable named like the secret in upper case, with every character other than a letter or a digit replaced by `_`, and then the name as written.
For example, the secret `model_gpt_5_5_api_key` is read from `MODEL_GPT_5_5_API_KEY`.
An empty value counts as not set.
A model secret that is not set falls back to the provider's own variable (here `OPENAI_API_KEY`).
Under `FLOW_PROFILE=production` only the named secret counts: a model that steps use directly stops start-up without it, and a fallback model such as `gpt-5.5` is left out.
The Gemini models read ADK's own `GOOGLE_API_KEY` (or Vertex AI credentials), not a named secret.
Set the variables in the process environment, for example from your platform's secret store.
They may also be in `retail_kyc_onboarding/.env` (see `retail_kyc_onboarding/.env.example`): the ADK tools, the FastAPI server and the run host all load that file, and a variable already set wins.
`EnvSecrets({"prefix": "APP_"})` reads `APP_` plus the upper-cased name, then the name as written.

## With another store

Choose another `secret` provider in `runtime.yaml`:

```yaml
provider-profile:
  default:
    factories:
      secret: {provider: google_secret_manager, project: my-project}
```

Built-ins: `EnvSecrets`, `FileSecrets`, `GoogleSecretManagerSecrets`, `HashicorpVaultSecrets` and `MockSecrets` (all in `retail_kyc_onboarding._engine.factories.secret`).
Settings are validated: a wrong key stops the start-up with the provider's name.
To write your own, subclass `retail_kyc_onboarding._engine.factories.secret.SecretFactory` in a module of your own (the `VaultSecrets` example in [EXTENDING.md](../EXTENDING.md)) and name it by class path in `runtime.yaml` ([configure providers](configure-providers.md#use-a-provider-of-your-own)).
