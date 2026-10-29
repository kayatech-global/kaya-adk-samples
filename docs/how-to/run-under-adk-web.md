# Run it with the ADK CLI and the ADK web UI

All commands run from the repository root, with the keys in `retail_kyc_onboarding/.env` (README, "Add your model keys").

## ADK CLI

```bash
adk run retail_kyc_onboarding                  # interactive conversation
adk run retail_kyc_onboarding --save_session   # keep the session in a file to inspect or --resume later
```

When a step pauses for input (a review gate, or a compliance review question), `adk run` prints the message and waits for your answer in the terminal.
The replies each pause expects, and example requests for every path, are in [Try the flows](try-the-flows.md).
The engine's trace records (one JSON line per event: step start and end, model call, route, authorization, tool call, ...) go to stderr; `adk run ... 2>/dev/null` hides them.

## ADK web UI

```bash
adk web                                # http://127.0.0.1:8000, every agent folder here (only retail_kyc_onboarding)
adk web --port 8001                    # another port
adk web retail_kyc_onboarding          # this agent only (ADK's single-agent mode)
```

The package turns on ADK's own switch for its "[EXPERIMENTAL]" warnings (`ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS`, in `__init__.py`) unless the environment already sets it, so `adk run`, the FastAPI server and every run are quiet.
`adk web` builds its server, and prints two such warnings about its credential service, before it loads any agent; set the variable in your shell to hide those too.

- The chat panel runs the workflow; a pause appears as a request for input that you answer in the chat.
- The graph view shows the exported workflow as ADK nodes: each Kaya step is a chain of nodes named `<step>__<phase>` (agent steps `prep`, `agent`, `post`; tool steps `approvals`, `run`; the external agent step `run`; with `gate`, `ask`, `receive`, `halt` and `__wait` nodes where a step pauses).
- The Events tab shows every event of a turn: the prompt sent to each model, tool calls and their results, and the branch taken.
- The Eval tab can record a conversation as an ADK eval case; this package's evals are the scripted flows in `tests/eval/` ([README](../../tests/eval/README.md)).

## How it loads

ADK imports `agent.py` and reads `app` (an ADK `App` around `root_agent`, with resumability on, so a paused run continues where it stopped).
`root_agent` and `app` are built on first read, because ADK imports the module inside its own event loop.
The agent starts without any key; a step needs its model's key only when it runs.
`.env` is loaded by ADK, and the providers and replacements in `settings.py` apply here too.
If the folder has no `.env`, ADK looks for one in the parent folders and uses the first it finds.

## What differs from the governed run host

- ADK's own session service holds the state (`--session_service_uri` chooses it), not the `session` provider.
- Tool-step approvals arrive as `adk_request_input` (answer `{"approved": true}` or `false`); approvals of model tool calls use ADK's tool confirmation (`adk_request_confirmation`), and this export has none.
- There is no authentication; keep `adk web` on your machine.
- `adk web` and `adk api_server` start a new trace for every request (ADK 2.12 continues only its Agent Engine trace header); `server.py` (README, "FastAPI server") serves the same API and keeps the caller's W3C trace.

The governed run host is what production exposes: see [INTEGRATION.md](../INTEGRATION.md#governed-run-host).
