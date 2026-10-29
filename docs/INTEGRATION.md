# Integrate Retail KYC Onboarding

The agent is an ordinary Google ADK app, so every ADK way of running or embedding an agent works.
All of them run the same workflow (`retail_kyc_onboarding/workflow/`) with the providers chosen in `retail_kyc_onboarding/settings.py`.

| Way | Entry point | Authentication | Use it for |
|-----|-------------|----------------|------------|
| ADK CLI and web UI | `adk run retail_kyc_onboarding`, `adk web` | none (local) | trying and debugging the workflow |
| ADK's REST API | `uvicorn retail_kyc_onboarding.server:app` | none | your own client on a trusted network, or behind a gateway |
| Your own ADK code | `from retail_kyc_onboarding.agent import app, root_agent` | yours | embedding in a larger ADK application |
| Governed run host | `uvicorn --factory retail_kyc_onboarding.server:create_run_host` | the `authn` provider | an exposed service: REST runs, tool approvals, A2A |

Every command runs from the repository root.
Keys go in `retail_kyc_onboarding/.env` (README, "Add your model keys"); every way above reads it.

## ADK's REST API

`server.py` is ADK's own FastAPI app (`get_fast_api_app`, what `adk api_server` serves, `GET /health` included).

```bash
uvicorn retail_kyc_onboarding.server:app --port 8000     # interactive docs at http://127.0.0.1:8000/docs
```

```bash
curl -s -X POST http://127.0.0.1:8000/apps/retail_kyc_onboarding/users/u1/sessions \
  -H 'content-type: application/json' -d '{"sessionId": "s1"}'
curl -s -X POST http://127.0.0.1:8000/run_sse -H 'content-type: application/json' -d '{
  "appName": "retail_kyc_onboarding", "userId": "u1", "sessionId": "s1", "streaming": false,
  "newMessage": {"role": "user", "parts": [{"text": "Open a Savings Account for Arturo Conroy, ID 57-607128-154274-1"}]}}'
```

`/run` returns all events of the turn at once; `/run_sse` streams them.
When the workflow pauses for input (a compliance decision, for example), the events end with ADK's standard human-input request: a long-running `adk_request_input` function call whose `message` is the question.
Answer it the ADK way, with a message holding a `functionResponse` for that call's `id` on the same session; `adk run` and `adk web` do this for you.
For more options (session database, the web UI, CORS), `server.create_app(...)` passes keyword arguments to `get_fast_api_app`; serve your own module with `app = create_app(web=True, session_service_uri="sqlite+aiosqlite:///sessions.db")`.

This API has no authentication: any caller can run the agent and read any session, as with `adk api_server`.
Keep it on localhost or behind an authenticating gateway; expose the governed run host instead.

## In your own ADK code

`root_agent` is a native ADK `Workflow` (each step a short chain of ADK nodes, `<step>__<phase>`) and `app` an ADK `App` with resumability on.
Both are built on first read, so importing the package needs no key or network.

```python
from google.adk.runners import InMemoryRunner
from google.genai import types

from retail_kyc_onboarding.agent import app

runner = InMemoryRunner(app=app)


async def ask(user_id: str, session_id: str, text: str) -> list[str]:
    if not await runner.session_service.get_session(app_name=app.name, user_id=user_id, session_id=session_id):
        await runner.session_service.create_session(app_name=app.name, user_id=user_id, session_id=session_id)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    out = []
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
        out += [p.text for p in (event.content.parts if event.content else []) if p.text]
    return out
```

To place the workflow inside a larger ADK application:

- **As a step of another workflow:** a `Workflow` is a node, so `Workflow(name="bank", edges=[("START", root_agent), ...])` works.
- **As a sub-agent of an `LlmAgent` coordinator:** use the run-host agent, an ADK `BaseAgent` whose every turn is a governed run (it does not authenticate; under `FLOW_PROFILE=production` set `FLOW_PRINCIPAL_FROM=state` or `user_id`):

  ```python
  from google.adk.agents import LlmAgent

  from retail_kyc_onboarding._engine.runtime.entry import flow_agent_for
  from retail_kyc_onboarding.agent import abuild_engine

  coordinator = LlmAgent(
      name="coordinator",
      model="gemini-3.5-flash",
      instruction="Delegate retail onboarding requests.",
      sub_agents=[flow_agent_for(abuild_engine, name="kyc_onboarding")],
  )
  ```

- **With different models, prompts or tools for one use:** `agent.build(models={"gemini_3_8_flash": my_llm})` returns a new `(root_agent, app)`; the same maps in `settings.py` apply everywhere.

`tests/integration/test_adk_native.py` loads the agent with ADK's own loader and runs it on ADK's `InMemoryRunner`.

## Governed run host

The run host adds what ADK's API does not: authentication through the `authn` provider, authorization on every action, tool approvals, runs that survive a restart once `FLOW_SESSION_URI` names a database, and a trace per run (`/v1/runs/{id}/trace`, recent runs held in this process's memory; a durable trace comes from the `telemetry` provider).
It also serves the A2A agent card.

```bash
uvicorn --factory retail_kyc_onboarding.server:create_run_host --port 8080
# docs at /docs, health at /healthz and /readyz, A2A card at /.well-known/agent-card.json
curl -s localhost:8080/v1/runs -H 'content-type: application/json' -d '{"message": "Open a Savings Account for Arturo Conroy, ID 57-607128-154274-1"}'
curl -s localhost:8080/v1/runs/<session_id>/trace
```

With the shipped defaults, `authn` is `anonymous` and `authz` is `allow_all` (fine on a laptop), runs live in memory and vanish on restart, secrets are read from environment variables, and the trace is printed to stderr.

```python
import httpx

with httpx.Client(base_url="http://localhost:8080") as http:
    run = http.post("/v1/runs", json={"message": "Open a Savings Account for Arturo Conroy, ID 57-607128-154274-1"}).json()
    session_id = run["session_id"]
    while run["status"] == "paused":
        if run["pause"]["kind"] == "input":  # a question: answer and resume
            answer = input(run["pause"]["message"] + " ")
            run = http.post(f"/v1/runs/{session_id}/resume", json={"text": answer}).json()
        else:  # an approval: an authorized approver POSTs to /v1/approvals/{approval_id}
            print("approval waiting:", run["pause"]["approval_id"])
            break
    print(run["status"], run.get("output"))
```

Before exposing it:

- Authentication: return an `authn` provider from `settings.build_providers(...)` (for example `JwtOidcAuthn` or `KeycloakStaticKeyAuthn`) (or `authn: jwt_oidc` in `runtime.yaml`) and set `FLOW_PROFILE=production`.
  Production refuses every caller of the anonymous provider (401), reports it under `unhealthy` with `/healthz` `degraded` and `/readyz` 503, and makes the guard deny when the `authz` provider fails.
- Approvals: authorized approvers `POST /v1/approvals/{approval_id}` with `{"approved": true, "comment": "..."}`.
- Durable runs: set `FLOW_SESSION_URI` (for example `sqlite+aiosqlite:///runs.db`, or `postgresql+asyncpg://...` after `pip install asyncpg`) so paused runs survive a restart ([settings](reference/settings.md)).
- Full API: [reference/api.md](reference/api.md).

### A2A

The run host publishes an A2A 0.3 agent card at `/.well-known/agent-card.json` and a JSON-RPC endpoint at `/a2a` (`message/send` and `tasks/get`; no streaming).
`contextId` is the session: send it again to answer a question.
Approvals are never answered over A2A (JSON-RPC error `-32004`, unsupported operation); an authenticated approver uses `POST /v1/approvals/{id}`.
Behind a proxy, start uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>` so the card names the public address.

A `message/send` request (`curl -s localhost:8080/a2a -H 'content-type: application/json' -d @request.json`):

```json
{"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {"kind": "message", "role": "user", "messageId": "m1",
  "parts": [{"kind": "text", "text": "Open a Savings Account for Arturo Conroy, ID 57-607128-154274-1"}]}}}
```

The result is a task: `status.state` is `input-required` while the run waits (the question is in `status.message`), `completed` with the answer in `artifacts`, or `failed`.
Any A2A 0.3 client works as well; the `a2a-sdk` package is not in `requirements.txt`.

## Tracing

Send a W3C `traceparent` header (and `tracestate`) to ADK's REST API or the run host.
The run joins your trace: ADK's spans, including `call_llm` for every model call, and the `traceparent` of every tool and partner-agent call carry your trace id, and the run host keeps it across pauses.
Spans are recorded once an OpenTelemetry SDK is configured: return `OtlpTelemetry` from `settings.build_providers(...)`, or set up the SDK yourself (ADK's `--otel_to_cloud` does it for Cloud Trace).
Plain `adk api_server` and `adk web` start a new trace per request; `server.py` does not.
`tests/integration/test_trace_context.py` checks both servers.

## Demo limits

Identity, entitlements, approvers, partner-agent credentials and secret values are not exported.
Until `settings.build_providers(...)` returns your own providers, callers are anonymous and every action is allowed; see [SECURITY.md](SECURITY.md).
