# Update prompts

A prompt is the instruction an agent step sends to the model.
This export has two, in `retail_kyc_onboarding/workflow/components/prompts.py` (generated from Kaya; do not edit it):

| Prompt id | Constant | Used by |
|-----------|----------|---------|
| `kyc_intake_agent` | `KYC_INTAKE_AGENT` | Extract KYC intake |
| `kyc_risk_agent_prompt` | `KYC_RISK_AGENT_PROMPT` | Assess KYC risk, Compliance approval review, Enhanced due diligence review, Summarize opened account, Return rejection or stop decision, Handle account creation failure |

Because six steps share the second prompt, a change to it reaches all of them.
What a step is asked to do right now is its task (`task=` in `workflow/graph.py`), added after the prompt; [update agents](update-agents.md) says which of the two to change.

There are four ways to change a prompt, from smallest to biggest.
Every example is a test (`tests/unit/test_doc_examples.py`).

## Replace the text of one prompt

In `retail_kyc_onboarding/settings.py`, add the prompt id to `PROMPTS` with the new text.
This changes the policy of the KYC Risk Agent so that a GDP growth below 1 (not below 0) is medium risk:

```python
from .workflow.components.prompts import KYC_RISK_AGENT_PROMPT

PROMPTS["kyc_risk_agent_prompt"] = KYC_RISK_AGENT_PROMPT.text.replace(
    "GDP growth is below 0", "GDP growth is below 1"
)
```

Starting from the exported text and replacing a phrase keeps everything else and makes the edit visible in a diff.
You can also write the whole text as a string.
The id is typed (`PromptId`), so a misspelt id is a mypy error, and at start-up a `ConfigError` that lists the known ids.
For one use in code, pass `agent.build(prompts={"kyc_risk_agent_prompt": "..."})`, which wins over `settings.py`.

Check it: the start-up trace event `engine.started` lists `prompt:kyc_risk_agent_prompt (override)` under `modified_entities`.

## Keep the placeholders

A `{name}` in a prompt is filled from the run's variables when the step runs:

| Placeholder | Filled from |
|-------------|-------------|
| `{kyc_intake_agent.full_name}`, `.id_number`, `.product`, `.email` | the intake step |
| `{id_registry_lookup.registered_name}`, `.registry_id_number`, `.dob`, `.address`, `.id_status`, `.nationality_code`, `.pep_flag`, `.sanctions_hit` | the registry lookup |
| `{world_bank_country_risk_agent.response}`, `.status`, `.artifacts` | the World Bank agent |
| `{account_creation.account_number}`, `.account_status`, `.created_customer_id_number` | account creation |

A placeholder with no variable stays as written in the text the model sees.
The exported risk prompt has one: `{country_risk_agent}` matches no variable, because the World Bank reply is `{world_bank_country_risk_agent.response}`.
You can fix that locally:

```python
PROMPTS["kyc_risk_agent_prompt"] = KYC_RISK_AGENT_PROMPT.text.replace(
    "{country_risk_agent}", "{world_bank_country_risk_agent.response}"
)
```

Do this only if you also make the change in Kaya: otherwise the package no longer matches the plan it was exported from.
The two prompts also tell the model to answer with a JSON object of exact keys; keep that part, because the step's structured output depends on it.

## Pin a whole prompt with its own version

Give a `Prompt` instead of text to name the version the trace reports:

```python
from ._engine.content import Prompt

PROMPTS["kyc_intake_agent"] = Prompt(
    id="kyc_intake_agent",
    version="v-2026-11",
    text=KYC_INTAKE_AGENT.text + "\n- Treat a missing email as null, never as an empty string.",
)
```

## Change it in Kaya

For a change that belongs in the workflow itself, edit the prompt on the Kaya agent node and export again.
A re-export brings the new text into `workflow/components/prompts.py`; keep your `settings.py` (a re-export leaves it alone) and your `runtime.yaml` ([re-export from Kaya](../../README.md#re-export-from-kaya)).
If you edit `prompts.py` by hand instead, re-record the prompt's `sha256` (print `digest(prompts.KYC_RISK_AGENT_PROMPT)` from `retail_kyc_onboarding._engine.content`, as for the graph in [update agents](update-agents.md#run-one-step-on-a-different-model)) and make the same change in Kaya; a `PROMPTS` entry avoids both.
