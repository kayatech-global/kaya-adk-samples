# Update agents

An agent step asks a model to do one job and writes its answer into variables.
This page says what each agent step is made of, which parts you can change in this package without Kaya, and which parts belong to the exported workflow.

## The agent steps

Seven steps call a model; the others call tools or a partner agent.
The table is checked against `workflow/graph.py` by a test.

| Step id | Prompt | Model | Review gate before it | Writes |
|---------|--------|-------|-----------------------|--------|
| `extract_kyc_intake` | `kyc_intake_agent` | `gemini_3_8_flash` | no | `kyc_intake_agent.*` |
| `assess_kyc_risk` | `kyc_risk_agent_prompt` | `gemini_3_8_flash` | no | `kyc_risk_agent.*` |
| `compliance_approval_review` | `kyc_risk_agent_prompt` | `gemini_3_8_flash` | yes | `kyc_risk_agent.*` |
| `enhanced_due_diligence_review` | `kyc_risk_agent_prompt` | `gemini_3_8_flash` | yes | `kyc_risk_agent.*` |
| `summarize_opened_account` | `kyc_risk_agent_prompt` | `gemini_3_8_flash` | no | `kyc_risk_agent.*` |
| `return_rejection_or_stop_decision` | `kyc_risk_agent_prompt` | `gemini_3_8_flash` | yes | `kyc_risk_agent.*` |
| `handle_account_creation_failure` | `kyc_risk_agent_prompt` | `gemini_3_8_flash` | yes | `kyc_risk_agent.*` |

The two compliance reviews are follow-up steps: the agent writes the question for the compliance officer, then the run waits for the reply, which the step's condition routes (approve, stop or ask again).

## What each part controls

Each agent step in `workflow/graph.py` is an `AgentConfig` (a follow-up wraps one in `FollowupConfig(agent=...)`):

| Part | What it does | Where it lives | Change it in |
|------|--------------|----------------|--------------|
| `prompt` | the standing instruction, shared by steps | `workflow/components/prompts.py` | `settings.py` (`PROMPTS`) or Kaya |
| `model` | which model answers, with its fallbacks | `workflow/components/models.py` | `settings.py` (`MODELS`) or Kaya |
| `task` | what this step must do now, added after the prompt | `workflow/graph.py` | Kaya (or a hand edit, below) |
| `structured_output` | the keys of the answer and the variables they fill | `workflow/graph.py` | Kaya (or a hand edit) |
| `interrupt` | the review gate before or after the step | `workflow/graph.py` | Kaya (or a hand edit) |
| tools | what the model may call; none of these steps has any | `workflow/graph.py` | Kaya |
| branch condition | which edge a step takes | `workflow/components/conditions.py` | Kaya |

`workflow/` is generated from the Kaya workflow.
A change that belongs to the workflow is made in Kaya and exported again; `settings.py` is yours and a re-export keeps it, so keep `runtime.yaml` with it ([re-export from Kaya](../../README.md#re-export-from-kaya)).

## Change what an agent decides

The decision policy is the KYC Risk Agent's prompt, so this is a prompt change:

```python
from .workflow.components.prompts import KYC_RISK_AGENT_PROMPT

PROMPTS["kyc_risk_agent_prompt"] = KYC_RISK_AGENT_PROMPT.text.replace(
    "GDP growth is below 0", "GDP growth is below 1"
)
```

It reaches all six steps that share the prompt, and the `assess_kyc_risk` condition then routes on the new answers.
More ways to change a prompt, and the placeholders: [update prompts](update-prompts.md).

## Change the model of every step

The steps name the model by id, so replacing the model replaces it everywhere:

```python
from dataclasses import replace

from .workflow.components.models import GEMINI_3_8_FLASH

MODELS["gemini_3_8_flash"] = replace(GEMINI_3_8_FLASH, model_name="gemini-3.5-flash", fallbacks=())
```

Fallbacks, a key of its own for one model and a gateway: [change the model](change-the-model.md).

## Run one step on a different model

All seven steps name `gemini_3_8_flash`, so a single step cannot be re-pointed from `settings.py`.
Do it in Kaya (set the model on that agent node) and export again, or by hand in `workflow/graph.py`, which marks the package as edited until you re-record the digest:

1. Change the step's `model=` to the id of another entity of `workflow/components/models.py`:

   ```python
   # in the node "summarize_opened_account"
   model="gpt_5_5",   # was "gemini_3_8_flash"
   ```

2. Re-record the digest: from the repository root, print the new value and paste it over `GRAPH_SHA256` at the end of `workflow/graph.py`:

   ```bash
   python -c "from retail_kyc_onboarding._engine.content import digest; from retail_kyc_onboarding.workflow.graph import GRAPH; print(digest(GRAPH))"
   ```

3. Make the same change in Kaya, or the next export reverts it.

`gpt_5_5` needs `OPENAI_API_KEY` (or the secret `model_gpt_5_5_api_key`).

## Add or remove a review gate

A gate is the `interrupt=InterruptConfig(before=InterruptSide(enabled=True, ...))` of an agent step.
This is what `return_rejection_or_stop_decision` has, and `assess_kyc_risk` does not:

```python
interrupt=InterruptConfig(
    before=InterruptSide(
        enabled=True,
        message="Your application is being reviewed by our compliance team. We’ll continue once the review is complete.",
        actions=[{"label": "Proceed", "value": "proceed"}, {"label": "Stop", "value": "stop"}],
    ),
),
```

To remove the gate before a step, delete that argument from its `AgentConfig`; to add one, paste it in.
Then re-record the digest, make the change in Kaya too, and update `tests/eval/flows.json` and [try the flows](try-the-flows.md), because the replies of a flow change with its gates.
In Kaya the gate is set on the agent node, so it fires before every step that node runs; this export puts it per step, which is why the exported flow differs from the platform's here.

## Change what a step is told to do

The task and the shape of the answer are part of the exported step.
Change them on the Kaya agent node and export again.
A hand edit of `task=` in `workflow/graph.py` needs the same three steps as above (edit, re-record the digest, change it in Kaya).
If the change is about wording or policy, the prompt is the smaller change.
