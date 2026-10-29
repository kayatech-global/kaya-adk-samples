# Tools

| Tool | Kind | Version | Call | Parameters | Approval |
|------|------|---------|------|------------|----------|
| `retail_kyc_account_creation` | REST API | `v-e72bdb34` | POST https://608b7a57737e470017b74d29.mockapi.io/api/v1/accounts | customer_id_number, full_name, product, status | none (`never`) |
| `retail_kyc_id_registry_lookup` | REST API | `v-f8c55046` | GET https://608b7a57737e470017b74d29.mockapi.io/api/v1/customers | id_number | none (`never`) |

Definitions are `ToolDef` constants in `retail_kyc_onboarding/workflow/components/tools.py`: `RETAIL_KYC_ACCOUNT_CREATION`, `RETAIL_KYC_ID_REGISTRY_LOOKUP`.
Change one under `content.tools` in `runtime.yaml`, or in `TOOLS` in `settings.py` ([update tools](../how-to/update-tools.md)).
How a call is made is decided by the `api` and `mcp` providers; credentials by the `credentials` provider.

## Models

| Model id | Version | Provider | Model |
|----------|---------|----------|-------|
| `gemini_3_8_flash` | `v-gemini-3-8-flash` | google | gemini-3.8-flash (every model step) |
| `gemini_3_5_flash` | `v-gemini-3-5-flash` | google | gemini-3.5-flash (first fallback) |
| `gpt_5_5` | `v-261b6b2b` | openai | gpt-5.5 (last fallback, only with an OpenAI key) |

## Prompts

| Prompt | Version | Used by step |
|--------|---------|--------------|
| `kyc_intake_agent` | `v-7b565b42` | `extract_kyc_intake` |
| `kyc_risk_agent_prompt` | `v-323d55c3` | `assess_kyc_risk`, both reviews, `summarize_opened_account`, `return_rejection_or_stop_decision`, `handle_account_creation_failure` |
