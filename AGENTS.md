# AGENTS.md

Context for coding agents working on this agent: what it is, how to set it up, where to change what, and the rules that keep it working.
People should start with `README.md`; this file covers the same ground, condensed.

## What this is

A repository holding one Google ADK 2.12 agent folder, `retail_kyc_onboarding/`, exported from the Kaya workflow "Retail KYC Onboarding".
It runs on its own: Python and the packages in `retail_kyc_onboarding/requirements.txt`, with no connection to Kaya at run time.
It is a minimal implementation for validating Kaya-generated agents against ADK standards: it replicates the Kaya graph with ADK's graph `Workflow`, uses no ADK annotations or advanced patterns, and deliberately leaves out monitoring, guardrails and audit trails (`README.md`, "Scope of Step 1"; Step 2 adds them).

The flow onboards a retail banking customer:
extract the applicant's details, look them up in the ID registry, get World Bank country context over A2A, score KYC risk, route to a standard or enhanced due diligence review by a compliance officer, then open the account or explain the rejection.
Every path, with real registry data and the replies to give, is in `docs/how-to/try-the-flows.md` and `tests/eval/flows.json`.

## The concept

The package `retail_kyc_onboarding/` has three layers, and each has one owner (the folders and files below are inside it):

| Layer | Folder | Owner | What it holds |
|-------|--------|-------|---------------|
| The workflow | `workflow/` | Kaya (generated) | `graph.py` (steps and edges) and `components/` (prompts, models, tools, branch conditions, guardrails), each constant with a `sha256` digest |
| The engine | `_engine/` | the exporter (vendored) | the runtime that turns the graph into an ADK `Workflow`, the guard, the providers, the run host |
| The configuration | `runtime.yaml`, `settings.py` | you | which provider serves each component, and replacements for single prompts, models and tools |

Every external component (secrets, identity, entitlements, models, prompts, tool calls, sessions, telemetry, approvals, ...) is a factory kind with a base class, and a provider is one implementation of it.
`runtime.yaml` names, per mode, the provider of each kind: `default` runs locally with the built-ins, `arc` selects the empty placeholder providers of `arc_providers.py`, an optional extension point for an ARC integration that nothing uses unless you select it.
`FLOW_PROVIDER_MODE` picks the mode.
Its `content:` section can route one prompt, model or tool to another provider, or override its fields, by entity id.
Code in `settings.build_providers()` and the `MODELS`, `PROMPTS`, `TOOLS` maps in `settings.py` wins over the file.
The graph names prompts, models and tools by id only, and each id is resolved through its factory at run time, so switching a source is configuration, not a code change.

`agent.py` exposes `root_agent` (an ADK `Workflow`) and `app` (an ADK `App`, resumable), both built on first read.

## Set up

Run every command from the repository root (the folder that holds this file).

```bash
python3 -m venv .venv && source .venv/bin/activate      # Python 3.12 - 3.14
pip install -r requirements.txt                          # the agent's requirements plus pytest and pytest-asyncio
cp retail_kyc_onboarding/.env.example retail_kyc_onboarding/.env   # then set GOOGLE_API_KEY
```

- `GOOGLE_API_KEY` is required for real runs: every model step runs on Gemini `gemini-3.8-flash`.
- `OPENAI_API_KEY` (or `MODEL_GPT_5_5_API_KEY`) is optional: it adds `gpt-5.5` as the last fallback.
- The offline tests need no key.
- `arc` mode is optional and runs only once the placeholders in `arc_providers.py` are implemented (`README.md`, "Extending for ARC").
- `.env` stays inside the agent folder, where ADK and `settings.py` look for it.
- `.env` and `.venv/` are git-ignored and stay out of every deploy image.

## Commands

```bash
pytest tests                                         # offline: mock models and tools, no keys
FLOW_LIVE_EVALS=1 pytest tests/eval -m live          # every flow with the real model and services (GOOGLE_API_KEY in retail_kyc_onboarding/.env)
adk run retail_kyc_onboarding                        # terminal conversation
adk web                                              # developer UI on :8000 (add --port 8001 if it is taken); select retail_kyc_onboarding
uvicorn retail_kyc_onboarding.server:app --port 8000                           # ADK's REST API (with /health)
uvicorn --factory retail_kyc_onboarding.server:create_run_host --port 8080     # the governed run host
python deployment/deploy.py list                     # deploy targets; files only unless --apply
```

## Layout

```
.
├── retail_kyc_onboarding/   the ADK agent folder (what `adk run`, `adk web` and `adk deploy` load)
│   ├── __init__.py          from . import agent (how ADK finds the agent)
│   ├── agent.py             root_agent and app, what the ADK tools load
│   ├── settings.py          settings, build_providers(...), MODELS / PROMPTS / TOOLS replacements
│   ├── runtime.yaml         the provider of each kind and entity, per mode
│   ├── server.py            ADK's FastAPI app; create_run_host for the governed run host
│   ├── workflow/            generated from Kaya: graph.py and components/
│   ├── _engine/             vendored runtime
│   └── requirements.txt     runtime dependencies (what `adk deploy` installs)
├── requirements.txt         the agent's requirements plus the test tools
├── deployment/deploy.py     deploy targets (container, Kubernetes, Cloud Run, GKE, Agent Engine)
├── tests/                   unit and integration (offline); eval/ (live, opt-in)
└── docs/                    how-tos and reference (docs/index.md)
```

Outside this section and the commands, a bare file name such as `settings.py`, `runtime.yaml` or `workflow/` is the one in `retail_kyc_onboarding/`.

## Where to change what

| To change | Edit | Doc |
|-----------|------|-----|
| which provider serves a kind, per mode | `runtime.yaml` `factories:` | `docs/how-to/configure-providers.md` |
| one prompt, model or tool (text, model name, URL, auth, approval) | `runtime.yaml` `content:`, or `PROMPTS` / `MODELS` / `TOOLS` in `settings.py` | `docs/how-to/update-prompts.md`, `change-the-model.md`, `update-tools.md` |
| a provider built in code | `build_providers(...)` in `settings.py`, or a provider class of your own | `docs/EXTENDING.md` |
| an ARC integration (optional) | the placeholder classes in `arc_providers.py`, and the `arc` profile of `runtime.yaml` | `README.md` ("Extending for ARC") |
| a deploy target | `deployment/deploy.py` (`register_target`) | its docstring |
| steps, edges, tasks, gates, conditions | in Kaya, then export again | `docs/how-to/update-agents.md` |

`_engine/` is not edited: a change there is lost on the next export.
`workflow/` is changed in Kaya.
A hand edit there is reported as `<kind>:<id> (edited)` in the trace and fails `tests/unit/test_layout.py` until its digest is re-recorded.
To re-record one, from the repository root, print the new value and paste it over the old `sha256` (here the graph's `GRAPH_SHA256`):

```bash
python -c "from retail_kyc_onboarding._engine.content import digest; from retail_kyc_onboarding.workflow.graph import GRAPH; print(digest(GRAPH))"
```

Prefer a replacement in `runtime.yaml` or `settings.py`: it needs no digest and survives a re-export.

## Differences from the Kaya plan

Three deliberate differences keep the flows working; make the same changes in Kaya before exporting again, or a re-export reverts them:

- "Look up ID registry" maps the registry's first record (`{"results[0]": {...}}`), because the registry answers with a JSON array; the plan expects `{"data": {...}}` and maps nothing.
- Its condition also rejects a request with no ID, or with a registry ID that differs from the supplied one (compared with case, spaces and punctuation ignored).
- Every model step runs on Gemini `gemini-3.8-flash`, with `gemini-3.5-flash` and then `gpt-5.5` as fallbacks; the plan uses OpenAI `gpt-5.5` and has no fallbacks.

## How a run behaves

- Plan steps read the conversation, as Kaya workers read the run's messages: the user's messages, earlier tasks and outputs, then the step's task (`flow_conversation` in session state).
- Pauses are ADK `RequestInput` (`adk_request_input`); a client answers with a `FunctionResponse` of the same id, `{"result": "<reply>"}`.
- A review gate sits before the KYC Risk Agent writes a review question, a rejection or an account-failure answer (not before Assess KYC risk or the opened-account summary).
  It continues only when the whole reply is affirmative (`Proceed`, `yes`, `approve`, `continue`, ...); any other reply stops the run, as in Kaya.
- A compliance review is a follow-up step: the KYC Risk Agent writes the question, and the officer's reply is routed by the step's condition: a stop phrase stops, an approve phrase goes on to create the account, anything else asks again (the phrases are matched anywhere in the reply, even inside other words).
- Structured-output arrays and objects are requested as JSON text and decoded before they reach the variables.
- A model call that fails with HTTP 429, 500, 502, 503 or 504 (a 500, 502 or 503 after up to three attempts) moves on through ADK's `FallbackModel` to Gemini 3.5 Flash, then to `gpt-5.5` only when an OpenAI key is set; every model call has a five-minute deadline.

## Rules

- Every step goes through the engine's guard, which authorizes it and records the decision in the trace; do not call a model or tool around it.
- Secrets are logical names (`SECRETS` in `workflow/__init__.py`); never put a value in a file, prompt, log or trace.
- Branch conditions (`workflow/components/conditions.py`) are checked against an allow-list and compiled in a sandbox: no imports except `re` (only its pattern functions), and attributes only as calls to safe methods.
- `adk run` and `adk web` import `agent.py` inside their event loop: keep `root_agent` and `app` built on first read, never during the import.
- `__init__.py` turns on ADK's switch for its experimental-feature warnings unless the environment sets it; `tests/integration/test_no_warnings.py` keeps building and serving the agent warning-free.
- ADK's REST API and `adk web` have no authentication; the governed run host authenticates through the `authn` provider, and `FLOW_PROFILE=production` refuses the default anonymous one (`docs/SECURITY.md`).
- No ARC contract is assumed: the `arc` providers are empty, optional placeholders that raise `NotImplementedError` until someone implements them; `tests/unit/test_arc_placeholders.py` checks that the `arc` profile selects them.
- ADK resume replays a node, so tool calls must be idempotent or behind the review.
- The live evals call real services and open real accounts in the mock accounts API; keep them opt-in.

## Check a change

- Add a test beside the behaviour you change; tests build the engine with `retail_kyc_onboarding._engine.testing.offline.offline_providers`, and their settings live in `tests/conftest.py` (there is no pytest configuration file).
- `pytest tests` must stay green.
  It also runs every worked example of the how-tos (`tests/unit/test_doc_examples.py`), the documented A2A request, and every relative link of the docs (`test_doc_links.py`), so update a doc in the same change as the code it describes.
- For a change to a flow, run the live evals and update `tests/eval/flows.json` and `docs/how-to/try-the-flows.md` together.

## Pointers

`README.md`, `docs/index.md` (all documentation), `docs/how-to/try-the-flows.md`, `docs/how-to/configure-providers.md`, `docs/EXTENDING.md`, `docs/reference/settings.md`, `docs/SECURITY.md`, `tests/eval/README.md`.
