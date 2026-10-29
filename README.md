# Sample Kaya ADK Agent - Retail KYC Onboarding Usecase

A Sample Google ADK agent that onboards a retail banking customer: it extracts the applicant's details, checks the ID registry and country risk, scores KYC risk, routes the case to compliance review, and opens the account or explains the rejection.

**Source:** the Kaya workflow "Retail KYC Onboarding", exported to Google ADK `2.12.0`.
It runs on its own: Python and the packages in `retail_kyc_onboarding/requirements.txt`, with no connection to Kaya at run time.

## About this export: Step 1 of 2

This is a lightweight, independently executable Google ADK agent.
It runs an agentic graph that Kaya generated from a simple use case designed in Kaya.

It is the first step in validating Kaya-generated agents within Citi's existing technology and governance framework: an initial ADK compatibility and compliance validation.
You can run and inspect the generated code yourself, check it against Citi's internal ADK development standards, and take it through Citi's existing security, compliance and architecture review processes.
The aim is a quick technical validation that Kaya-generated agents can operate within Citi's approved technology framework.

The Kaya design is replicated as ADK's graph pattern: a `google.adk.workflow.Workflow` in which each of the workflow's steps is a short chain of ADK nodes (model calls as `LlmAgent`s, the others as `FunctionNode`s), wired along the Kaya design's edges and branches.
The graph is plain data in `retail_kyc_onboarding/workflow/graph.py`, and the engine in `retail_kyc_onboarding/_engine/` assembles it.

### Scope of Step 1

> **Step 1 is deliberately minimal.**
> Its only purpose is to establish that a Kaya-generated agent runs on ADK inside Citi's AI platform and passes Citi's reviews.
> To keep it small, quick to review and easy to trace back to the Kaya design, the capabilities below are intentionally left out of this sample.
> They are known, planned, and delivered in **Step 2: Full Kaya Capability Validation**, on the same business process.

| Left out of Step 1 | What this sample does instead | In Step 2 |
|---|---|---|
| **Kaya runtime governance**: dynamic graph adaptation, drift detection, governed replanning, execution auditing | Runs the exported graph as designed, with no adaptation or replanning | The Kaya governed execution capabilities, in a more comprehensive agent toolkit |
| **Enterprise operations**: monitoring, guardrails, audit trails | The trace goes to the console only; no guardrails are defined; no durable audit record is kept | Provided, or delegated to Citi's platform services |
| **Production identity and permissions** | Callers are anonymous and every action is allowed | Integrated with Citi's identity and entitlement services |
| **Common and utility components**: shared libraries and cross-cutting helpers a production toolkit factors out | Only what this one workflow needs is included | Shared components across agents |
| **ADK development best practices**: ADK annotations (nodes and tools defined with decorators) and ADK's advanced patterns (sequential, parallel and loop agents, sub-agents and agent transfer, agents used as tools) | Plain-data graph, a one-to-one mirror of the Kaya design, so it can be inspected against it step by step | Aligned with ADK's recommended development practices and Citi's ADK standards |

Monitoring, audit, identity and permissions already have extension points in this sample: provider kinds chosen in `runtime.yaml`, so Citi's platform (ARC) can plug in without changing the workflow ([Extending for ARC](#extensibility---support-for-enterprise-frameworks-such-as-arc), [Security](docs/SECURITY.md)).

## Quick start

The agent is the folder `retail_kyc_onboarding/`, a standard ADK agent folder (`__init__.py`, `agent.py` with `root_agent`, `requirements.txt`, `.env`).
Run every command below from the repository root, the folder that holds this README.

### 1. Install

Python 3.12 - 3.14:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The root `requirements.txt` installs the agent's own `retail_kyc_onboarding/requirements.txt` (all that running it needs, and what `adk deploy` installs) plus the test tools.
`.venv/` is git-ignored.

### 2. Add your model keys

```bash
cp retail_kyc_onboarding/.env.example retail_kyc_onboarding/.env
```

Open `retail_kyc_onboarding/.env` and set:

| Variable | Why | Where to get it |
|----------|-----|-----------------|
| `GOOGLE_API_KEY` | Every model step runs on Gemini `gemini-3.8-flash` | [aistudio.google.com](https://aistudio.google.com/apikey) |
| `OPENAI_API_KEY` (optional) | The last fallback, `gpt-5.5` | [platform.openai.com](https://platform.openai.com/api-keys) |

When Gemini 3.8 Flash is overloaded or down (HTTP 429, 500, 502, 503 or 504), a call moves on to Gemini 3.5 Flash with the same key, then to OpenAI `gpt-5.5` if its key is set ([Change the model](docs/how-to/change-the-model.md)).
"Retrieve country risk context" calls the public World Bank agent over A2A (`https://agentbazaar.tech/a2a/connectors/worldbank`, no key).

The file stays inside the agent folder, where ADK looks for it; it is git-ignored and is read by every way of running the agent below.
To run on Vertex AI or another provider, see [Change the model](docs/how-to/change-the-model.md); all other settings are optional ([settings reference](docs/reference/settings.md)).

### 3. Run it

`adk web` and the FastAPI server listen on port 8000; if it is taken, add `--port 8001` and use that port in the URLs below.

**ADK web UI**, the developer UI with the graph, the events of every step and the trace:

```bash
adk web --port 8001
```

Open http://127.0.0.1:8001 and select the app `retail_kyc_onboarding`, the only agent ADK finds in this repository.

Then type a request, for example `Open a Savings Account for Arturo Conroy, ID 57-607128-154274-1, arturo.conroy@example.com`.
When the workflow pauses for a compliance decision, answer in the same chat.
[Try the flows](docs/how-to/try-the-flows.md) has a request and the replies for each path the live services take, run against the mock ID registry.

**ADK CLI**, a conversation in the terminal:

```bash
adk run retail_kyc_onboarding
```

**FastAPI server**, ADK's REST API for your own client (interactive docs at http://127.0.0.1:8000/docs):

```bash
uvicorn retail_kyc_onboarding.server:app --port 8000
```

In another terminal, create a session and send a request:

```bash
curl -s -X POST http://127.0.0.1:8000/apps/retail_kyc_onboarding/users/u1/sessions \
  -H 'content-type: application/json' -d '{"sessionId": "s1"}'
curl -s -X POST http://127.0.0.1:8000/run -H 'content-type: application/json' -d '{
  "appName": "retail_kyc_onboarding", "userId": "u1", "sessionId": "s1",
  "newMessage": {"role": "user", "parts": [{"text": "Open a Savings Account for Arturo Conroy, ID 57-607128-154274-1"}]}}'
```

### 4. Run the tests

Offline, with mock models and tools, no keys needed:

```bash
pytest tests
```

The live evals run every flow against the real model and services ([tests/eval](tests/eval/README.md)).

## Extensibility - Support for Enterprise frameworks such as ARC

Prompts, models, tools, secrets, identity and permissions are not wired into the graph.
A step names them by id, for example `prompt="kyc_risk_agent_prompt"` and `model="gemini_3_8_flash"` in `workflow/graph.py`.
At run time the engine asks the provider of each kind for them, and `runtime.yaml` chooses the provider.
That is where ARC fits in: as providers, with no change to the workflow, `settings.py` or the engine.

The interfaces through which ARC exposes these services (endpoints, payloads, authentication and error handling) are defined and owned by Citi, and were not available to us when this sample was prepared.
Rather than assume a contract that may not match ARC, this export leaves the integration open: `retail_kyc_onboarding/arc_providers.py` provides one placeholder for each kind of service ARC could serve, ready to be implemented against ARC's actual interfaces:

| Kind | Placeholder | Implement |
|------|-------------|-----------|
| `secret` | `ArcSecrets` | `get(name)`: the value of a logical secret name |
| `authn` | `ArcAuthn` | `authenticate(request)`: the `Principal` calling the run host |
| `authz` | `ArcAuthz` | `check(principal, action, resource, context)`: allow, deny or require approval |
| `credentials` | `ArcCredentials` | `get(target, principal)`: the headers an outbound call carries |
| `prompt` | `ArcPrompts` | `get(prompt_id)`: the text of a prompt |
| `model` | `ArcModels` | `create(spec)`: the ADK model for a model id |
| `api` | `ArcApiFactory` | `build(tool)`: a handle that executes a REST tool |
| `mcp` | `ArcMcpFactory` | `build(tool)`: the tools of an MCP server |

The comments in each method give the engine's side of the contract: what it receives and what it must return.

To integrate:

1. Implement the methods of the kinds ARC should serve, replace their `health()` with a real check that ARC can be reached (until then `/readyz` answers 503), and delete the classes it should not.
2. Declare the settings your implementation needs on `ArcSettings` (a base URL, for example), and give them values in the `arc` profile of `runtime.yaml`.
3. Run with `FLOW_PROVIDER_MODE=arc`, for example `FLOW_PROVIDER_MODE=arc adk run retail_kyc_onboarding`.

The shipped `runtime.yaml` already wires the placeholders in:

```yaml
plugins: [retail_kyc_onboarding.arc_providers]  # imports the module, which registers the `arc` classes

provider-profile:
  arc:
    config: {}   # the fields of ArcSettings, e.g. {url: "${env:ARC_BASE_URL}"}; given to every `arc` provider
    factories:
      all: arc   # every kind that has an `arc` provider uses it; the other kinds stay as in `default`
```

Until a method is implemented, it raises an error that names it, for example `NotImplementedError: arc model provider: ArcModels.create is a placeholder; implement it in arc_providers.py`.
`ArcModels.create` and `ArcApiFactory.build` raise it at start-up; the others raise it when a run first calls them.

ARC can also take over only part of a kind.
In the `arc` profile, one prompt, model or tool can keep another provider while the rest go to ARC:

```yaml
    content:
      prompts:
        kyc_risk_agent_prompt: {provider: content}       # stays the exported prompt
      tools:
        retail_kyc_id_registry_lookup: {provider: http}  # called directly, not through ARC
```

ARC's own ADK plugins (policy or audit, for example) are named in the `adk_plugin` kind: `adk_plugin: {provider: static, plugins: ["pkg.module:Class"]}`.
[Configure providers](docs/how-to/configure-providers.md) covers routing and field overrides, and [Extending](docs/EXTENDING.md) covers writing and testing a provider.

## Re-export from Kaya

`retail_kyc_onboarding/workflow/` and `retail_kyc_onboarding/_engine/` are generated: change the workflow in Kaya and export again.
Keep your own changes in `retail_kyc_onboarding/settings.py`, `retail_kyc_onboarding/runtime.yaml`, modules of your own beside them, `deployment/deploy.py` targets and your own tests, and carry them over to the new export.
An id in `settings.py` or `runtime.yaml` that the new export no longer has stops the start-up with an error that lists the known ids.
A hand edit to a workflow constant is reported as `<kind>:<id> (edited)` in the trace and fails `tests/unit/test_layout.py`; replace it in `settings.py` or `runtime.yaml` instead.

## What is in this repository

```
.
├── README.md                    this file
├── AGENTS.md                    the same ground, condensed, for coding agents
├── requirements.txt             the agent's requirements plus the test tools
├── retail_kyc_onboarding/       the ADK agent folder: what `adk run`, `adk web` and `adk deploy` load
│   ├── __init__.py              from . import agent (how ADK finds the agent)
│   ├── agent.py                 root_agent (an ADK Workflow) and app, what the ADK tools load
│   ├── settings.py              what you change: settings, providers, model / prompt / tool replacements
│   ├── server.py                the FastAPI server
│   ├── runtime.yaml             which provider serves each kind and entity, per mode (default, arc)
│   ├── arc_providers.py         empty placeholder `arc` providers: where an ARC integration plugs in
│   ├── workflow/                the workflow exported from Kaya, never edited
│   │   ├── graph.py             the core definition: steps and edges
│   │   └── components/          what the graph refers to: prompts, models, tools, branch conditions, guardrails
│   ├── _engine/                 the runtime the workflow runs on (vendored, not edited)
│   ├── requirements.txt         runtime dependencies (what `adk deploy` and the container image install)
│   └── .env.example             copy to .env beside it and add your keys
├── deployment/deploy.py         container, Kubernetes, Cloud Run, GKE, Agent Engine
├── tests/                       offline unit and integration tests; tests/eval: live evals of every flow
└── docs/                        everything else
```

In the rest of this README and in `docs/`, a bare file name such as `settings.py`, `runtime.yaml` or `workflow/` is the one in `retail_kyc_onboarding/`.

## Learn more

| | |
|---|---|
| [What it does](docs/reference/workflow.md) | every step, edge, tool, prompt and model |
| [Try the flows](docs/how-to/try-the-flows.md) | a request and the replies for each path the live services take, run against the mock ID registry |
| [Run with the ADK CLI and web UI](docs/how-to/run-under-adk-web.md) | what the UI shows and how pauses work there |
| [Integrate](docs/INTEGRATION.md) | embed the agent in another ADK app, call the REST API, the governed run host and A2A |
| [Security](docs/SECURITY.md) | demo limits and the checklist before real use |
| [Configure providers](docs/how-to/configure-providers.md) | `runtime.yaml`: pick, route and override providers without code |
| [Update tools](docs/how-to/update-tools.md) | a tool's URL, authentication, timeout and approval |
| [All documentation](docs/index.md) | |

**Demo export:** callers are anonymous and every action is allowed until `build_providers(...)` in `settings.py` returns your own identity providers; see [Security](docs/SECURITY.md).
