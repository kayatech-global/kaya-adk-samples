# Security

## Reporting

Report a vulnerability in this package through your organisation's security channel, not a public issue.
Include the export id and version (`plan-retail_kyc_onboarding`, `export-0001`), the engine version (`0.1.0`) and steps to reproduce.

## Model of the system

- The governed run host (`server.create_run_host`) authenticates every request through the `authn` provider and authorizes every action through the `authz` provider; both are chosen in `build_providers(...)` in `settings.py`.
- ADK's own API (`server.app`, `adk api_server`, `adk web`, and the images `adk deploy` builds) has no authentication: any caller can run the agent and read any session.
  Keep it on localhost or behind an authenticating gateway; the `docker` and `kubernetes` targets in `deployment/deploy.py` run the governed run host by default, while `cloud_run` and `gke` use `adk deploy` and serve ADK's API, and `agent_engine` serves through Vertex AI Agent Engine.
- `FLOW_PROFILE=production` refuses every caller of the anonymous `authn` provider (401) and keeps the run host not ready (`/readyz` 503) until a real one is configured.
  In production an unhealthy `authn` or `authz` provider fails `/readyz`, and the guard denies an action when the `authz` provider fails.
- Secrets are referred to by logical name.
  Values come from the `secret` provider at run time and are never written to a file, a trace or a log.
- Tools that change state can require approval.
  The approver is resolved by a provider, and the requester can be barred from approving their own request.
- Approvals are never answered over the agent-to-agent (A2A) surface, and background waits are completed only by the server.
- Branch conditions are generated code held as source in `workflow/components/conditions.py`.
  They are checked against an allow-list of Python syntax and compiled in a sandbox at start-up, and a violation stops the start-up.

## Supply chain

- The engine (version 0.1.0) is vendored in `retail_kyc_onboarding/_engine` and depends on nothing from the platform that exported it.
- Every workflow constant carries the digest of its value.
  The engine recomputes it at start-up and reports a hand edit as `prompt:<id> (edited)`, and a replacement from `settings.py` as `model:<id> (override)`, in the trace; `tests/unit/test_layout.py` fails on a hand edit.
- `retail_kyc_onboarding/.gitignore` keeps the local `.env` and ADK's `.adk/` session store out of git and out of every `adk deploy` image.
- Check the requirements with `pip-audit -r retail_kyc_onboarding/requirements.txt`; `cyclonedx-py requirements retail_kyc_onboarding/requirements.txt` writes a CycloneDX SBOM.

## Hardening checklist before production

- Expose the governed run host, not ADK's API, or put an authenticating gateway in front.
- Set `FLOW_PROFILE=production` and return real `authn` and `authz` providers from `build_providers(...)`.
- If you implement the optional `arc` placeholders (`arc_providers.py`) for `authn`, `authz` or tool calls, they take over these checks: hold them to the same standard, and make a tool call honour its idempotency key (`call.idempotency_key`; the built-in `http` provider sends it as `Idempotency-Key`).
- Give every secret of the export a value in your secret store ([bind secrets](how-to/bind-secrets.md)), through the `secret` provider.
- Restrict outbound hosts for tools and the model gateway at the network layer.
- Set `FLOW_SESSION_URI` to a database so paused runs survive a restart; for `adk deploy` (`cloud_run`, `gke`) set the target's `session_service_uri` (`python deployment/deploy.py cloud_run --set session_service_uri=...`).
- Send the trace to your log or observability platform (`telemetry` provider) and set retention.
