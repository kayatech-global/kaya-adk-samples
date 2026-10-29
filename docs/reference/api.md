# HTTP API of the governed run host

The run host is a FastAPI application: `uvicorn --factory retail_kyc_onboarding.server:create_run_host`, from the repository root.
(ADK's own REST API, `retail_kyc_onboarding.server:app`, is documented by ADK and at its own `/docs`.)
The live schema is at `/openapi.json` and interactive documentation at `/docs` and `/redoc`.
To save the schema, from the repository root: `python -c "import json; from retail_kyc_onboarding._engine.server import openapi_document; print(json.dumps(openapi_document(), indent=2, sort_keys=True))" > openapi.json`.

Authentication is the `authn` provider returned from `settings.build_providers(...)`; send what it expects (for `JwtOidcAuthn` and `KeycloakStaticKeyAuthn`, `Authorization: Bearer <token>`).

| Method and path | Purpose |
|-----------------|---------|
| `GET /healthz` | Liveness and provider health. `degraded` means a provider the run host needs is unhealthy: one named `arc`, or under `FLOW_PROFILE=production` the `authn` or `authz` provider (the anonymous one included); any other unhealthy provider is listed under `unhealthy` without changing the status. |
| `GET /readyz` | Readiness: 200 when the export is loaded and every provider it needs (as for `/healthz`) is healthy, otherwise 503. |
| `GET /v1/export` | What this export is: ids, versions, source, providers, entities modified or overridden since export. |
| `POST /v1/runs` | Start a run. Body: `message`, optional `session_id`, `variables` and `stream` (server-sent events). |
| `POST /v1/runs/{session_id}/resume` | Answer a paused run. Body: `text`, or `approved` and `comment`, optional `interrupt_id`. |
| `GET /v1/runs/{session_id}` | Status of a run: `idle` or `paused` with the pause. |
| `GET /v1/runs/{session_id}/trace` | The recorded steps, tool calls, routes and decisions of the session's most recent run, from this process's memory (the most recent 200 runs; lost on restart). |
| `GET /v1/approvals` | Pauses this caller may answer: their own open questions (`kind: input`) and the approvals they may decide (`kind: approval`). |
| `POST /v1/approvals/{approval_id}` | Approve or reject: `approved` is required, and leaving it out rejects. |
| `GET /.well-known/agent-card.json` (also `/.well-known/agent.json`), `POST /a2a` | Agent card and JSON-RPC (`message/send`, `tasks/get`) for other agents. Approvals are never answered here. |

## Run result

```json
{
  "status": "done",
  "session_id": "6b0f...",
  "run_id": "...",
  "output": "text of the final answer",
  "variables": {},
  "pause": null,
  "error": ""
}
```

`status` is `done`, `paused` or `error`.
When it is `paused`, `pause` describes what is being asked and `/resume` or `/approvals` answers it.

## Errors

Errors are `application/problem+json` ([RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)): `type`, `title`, `status`, `detail`, `instance`.
Every response carries an `x-request-id` header (the caller's, or a generated one).

| Status | Meaning |
|--------|---------|
| 401 | The `authn` provider rejected the credentials. |
| 403 | The `authz` provider denied a resume, a read, an approval, or a start on another caller's session. A denied start is a 200 with `"status": "error"` and `"error": "Denied: run.start (...)"`. |
| 404 | Unknown session or approval. |
| 409 | The run cannot take this action in its current state. |
| 422 | The request body does not match the schema. |
| 503 | `/readyz` only: not ready. |
