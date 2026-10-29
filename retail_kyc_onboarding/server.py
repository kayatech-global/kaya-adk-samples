"""FastAPI server for Retail KYC Onboarding: ADK's own API (what `adk api_server` serves, `/health` included).

    uvicorn retail_kyc_onboarding.server:app --port 8000

Endpoints are ADK's (interactive docs at /docs): `/list-apps`, `/apps/retail_kyc_onboarding/users/{user_id}/sessions`,
`/run` and `/run_sse`. A request with a W3C `traceparent` header is handled inside the caller's trace, so ADK's spans, the
model calls and the tool calls join it (`adk api_server` itself starts a new trace).

Like `adk api_server`, this API has no authentication: any caller can run the agent and read any session. Keep it on
localhost (uvicorn's default) or behind an authenticating gateway. The governed run host (authentication, authorization,
tool approvals API, A2A agent card) is the server to expose, and what the `docker` and `kubernetes` deploy targets run by default:
`uvicorn --factory retail_kyc_onboarding.server:create_run_host` (docs/SECURITY.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app

from ._engine.server import create_app as _create_run_host
from ._engine.trace.context import TraceContextMiddleware
from .agent import abuild_engine

AGENT_DIR = Path(__file__).resolve().parent  # ADK's single-agent mode: this folder is the only app


def create_app(*, web: bool = False, **options: Any) -> FastAPI:
    """ADK's API server for this agent; ``web=True`` adds the developer UI, ``options`` go to ``get_fast_api_app``."""
    api: FastAPI = get_fast_api_app(agents_dir=str(AGENT_DIR), web=web, **options)
    api.add_middleware(TraceContextMiddleware)

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return api


def create_run_host(**overrides: Any) -> FastAPI:
    """The governed run host (REST runs, approvals, A2A); takes the keyword arguments of ``settings.build_config``."""
    return _create_run_host(build=lambda sinks: abuild_engine(extra_sinks=sinks, **overrides))


app = create_app()
