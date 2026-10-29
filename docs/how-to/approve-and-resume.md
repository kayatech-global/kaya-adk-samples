# Approve and resume a paused run

A run pauses when a step asks a person a question, when a tool needs approval, or when an interrupt is configured.
The response to `POST /v1/runs` then has `"status": "paused"` and a `pause` object.

## Answer a question

```bash
curl -s localhost:8080/v1/runs/<session_id>/resume \
  -H 'content-type: application/json' -d '{"text": "the answer"}'
```

## Approve or reject a tool call

List what is waiting for you:

```bash
curl -s localhost:8080/v1/approvals
```

Answer:

```bash
curl -s localhost:8080/v1/approvals/<approval_id> \
  -H 'content-type: application/json' -d '{"approved": true, "comment": "ok"}'
```

Who may answer is decided by the `approver_resolver`; the channel that delivers the request is the `approval_channel`.

No tool in this export carries approval settings; the `approval_policy` provider decides.

## Notes

- A rejection skips the tool: the step ends with an error result (`<step>_is_error` true, output `Not run: approval ... rejected`), and the step's branches decide where the run goes; the run itself does not fail.
- Approvals are not answerable over the agent-to-agent surface.
- With the default `FLOW_SESSION_URI=memory://` a restart loses the paused run; set it to a database.

## The pauses of this workflow

| Pause | Where | What to reply |
|-------|-------|---------------|
| Review gate | Before the KYC Risk Agent writes a review question, a rejection or an account-failure answer (not before Assess KYC risk or the opened-account summary) | The whole reply must be `Proceed` (or `yes`, `approve`, `continue`, `go ahead`, ...) to continue; anything else stops the run, as on Kaya |
| Compliance review question | Compliance approval review, Enhanced due diligence review | A reply containing a stop phrase (stop, reject, decline, deny, hold, do not proceed, do not approve, not approved) rejects; else one containing approve, proceed, continue or go ahead goes on to create the account; anything else asks again. Phrases match anywhere in the text, even inside other words, so reply with plain `Approve` or `Stop` |

Each gate's message names the next step and its replies, and the review question is the KYC Risk Agent's assessment as text.
[Try the flows](try-the-flows.md) has the replies for every path.

## How this maps to ADK human input

Every pause is ADK's graph human-input pattern ([adk.dev/graphs/human-input](https://adk.dev/graphs/human-input/)): a workflow node returns `RequestInput`, which reaches the client as an `adk_request_input` function call, and the reply is routed to the next node (the default `rerun_on_resume=False`).
The client answers with a `FunctionResponse` of the same id and name, `{"result": "<reply>"}`; the app is resumable and the run's state is in the session, so a paused run continues where it stopped.
Interrupt ids are unique per pause (`<step>:<before|ask>:<n>`), and the run host accepts an answer only for the pause that is open.

- **Free text, no `response_schema`.** The gates and questions take free text and list the expected replies in the message, as ADK's `request_input` sample does.
  ADK 2.12 does not enforce an `enum` in a `response_schema`, and `adk web` does not render one as choices, while a type mismatch is a hard error; the step checks the reply itself.
- **Replies arrive exactly as typed.** On resume ADK json-parses a `"result"` string (`"1.50"` becomes `1.5`, `"null"` becomes nothing), so the run host sends free text JSON-encoded.
  `adk web` and `adk run` send the raw text; a reply that is valid JSON there may be read as JSON.
- **Model tool approvals** use ADK's tool confirmation (`tool_context.request_confirmation`, answered as `adk_request_confirmation`), with an approval id derived from the function call id so a replayed call keeps its approval ([adk.dev/tools-custom/confirmation](https://adk.dev/tools-custom/confirmation/)).
  This workflow has no tool that needs approval.
- **Resume replays nodes** ([adk.dev/runtime/resume](https://adk.dev/runtime/resume/)): a step's tool call is not repeated once its result is recorded, but keep side effects behind the review (here, "Create active account" runs only after the officer approves).
