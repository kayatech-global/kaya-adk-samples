"""The HTTP run host an export ships with (app/server.py): authentication through the export's own authn factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..engine.engine import Engine
from ..engine.host import RunHost
from ..errors import AuthenticationError
from ..version import CORE_VERSION
from .a2a import a2a_router
from .models import ExportInfo, Health, Problem, Ready
from .ring import RingBufferSink
from .routes import RunContext, run_router

DESCRIPTION = """
HTTP run host for one exported workflow on Google ADK.

* **Runs** start a workflow, return its result or stream its trace, and are continued when a person answers.
* **Approvals** are answered by an authorized approver; a tool that needs one does not run without it.
* **Agent card** and JSON-RPC let another agent call this export (approvals are never answered there).

Authentication is the export's `authn` provider (chosen in its `settings.py`); send the credentials it expects (a bearer token for `jwt_oidc` and
`keycloak_static_key`). Errors are `application/problem+json` (RFC 9457).
"""

TAGS = [
    {"name": "runs", "description": "Start, continue and inspect runs."},
    {"name": "approvals", "description": "Tool approvals waiting for a person."},
    {"name": "a2a", "description": "Agent-to-agent: agent card and JSON-RPC."},
    {"name": "ops", "description": "Health, readiness and what this export is."},
]

_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    422: "Unprocessable Entity",
    503: "Service Unavailable",
}


def _problem(status: int, detail: str, path: str, headers: dict[str, str] | None = None) -> JSONResponse:
    body = {"type": "about:blank", "title": _TITLES.get(status, "Error"), "status": status, "detail": detail, "instance": path}
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def openapi_document() -> dict[str, Any]:
    """The run host's OpenAPI schema, without loading an export (the routes do not depend on one)."""
    async def never(sinks: list[Any]) -> Engine:
        raise RuntimeError("the schema app is never started")

    return create_app(build=never).openapi()


def create_app(*, engine: Engine | None = None, build: Callable[[list[Any]], Awaitable[Engine]] | None = None) -> FastAPI:
    """Serve one export: a ready ``engine`` (tests, embedding), or ``build``, an async function of the extra trace sinks that
    starts the engine at start-up (what an export's ``server.py`` passes).

    Raises:
        ValueError: Neither or both are given.
    """
    if (engine is None) == (build is None):
        raise ValueError("create_app needs exactly one of engine and build")
    ring = RingBufferSink()
    holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        eng = engine if engine is not None else await build([ring])  # type: ignore[misc]
        if engine is not None:
            eng.tracer.sinks.append(ring)
        holder["engine"], holder["host"] = eng, RunHost(eng)
        app.state.engine = eng
        yield
        if engine is None:
            await eng.aclose()

    app = FastAPI(
        title="Workflow run host",
        summary="Run, approve and inspect one exported workflow.",
        description=DESCRIPTION,
        version=CORE_VERSION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        license_info={"name": "Proprietary", "identifier": "LicenseRef-Proprietary"},
    )

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_problem(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(exc.status_code, str(exc.detail), request.url.path, dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return _problem(422, detail, request.url.path)

    async def resolve(request: Request) -> RunContext:
        eng: Engine = holder["engine"]
        try:
            principal = await eng.factories.authn.authenticate({"headers": dict(request.headers)})
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
        return RunContext(eng, holder["host"], principal, ring)

    @app.get("/healthz", tags=["ops"], operation_id="healthz", summary="Liveness and provider health", response_model=Health)
    async def healthz() -> dict[str, Any]:
        eng: Engine = holder["engine"]
        unhealthy = {k: v for k, v in eng.factories.health.items() if v != "ok"}
        needed = {k: v for k, v in unhealthy.items() if k in eng.required_kinds}
        return {"status": "ok" if not needed else "degraded", "export": eng.unit.manifest.export.model_dump(), "unhealthy": unhealthy}

    @app.get(
        "/readyz",
        tags=["ops"],
        operation_id="readyz",
        summary="Readiness: the export is loaded and the providers it needs are healthy",
        response_model=Ready,
        responses={503: {"model": Problem, "content": {"application/problem+json": {}}}},
    )
    async def readyz() -> dict[str, Any]:
        eng: Engine | None = holder.get("engine")
        if eng is None:
            raise HTTPException(503, "the export is still loading")
        needed = {k: v for k, v in eng.factories.health.items() if v != "ok" and k in eng.required_kinds}
        if needed:
            raise HTTPException(503, f"providers not ready: {needed}")
        return {"ready": True, "export": eng.unit.manifest.export.model_dump()}

    @app.get("/v1/export", tags=["ops"], operation_id="get_export", summary="What this export is", response_model=ExportInfo)
    async def export_info() -> dict[str, Any]:
        eng: Engine = holder["engine"]
        m = eng.unit.manifest
        return {
            "export": m.export.model_dump(),
            "source": m.source.model_dump(),
            "target": m.target.model_dump(),
            "entities": m.entities,
            "children": m.children,
            "exclusions": m.exclusions,
            "modified_entities": eng.unit.modified_entities,
            "providers": eng.factories.describe(),
            "core": CORE_VERSION,
        }

    app.include_router(run_router(resolve), prefix="/v1")

    def card_info() -> dict[str, Any]:
        eng: Engine = holder["engine"]
        m = eng.unit.manifest
        name = str(getattr(m.source, "workflow_name", "") or m.export.id)
        return {
            "name": name,
            "description": f"{name}: an exported {m.source.kind.upper() if m.source.kind == 'iflow' else 'execution plan'} served as an A2A agent",
            "version": m.export.version,
            "securitySchemes": eng.factories.authn.a2a_security(),
        }

    app.include_router(a2a_router(resolve, card_info))

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
            tags=TAGS,
            license_info=app.license_info,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Used by the `jwt_oidc` and `keycloak_static_key` authn providers. Other providers read other headers; the anonymous provider needs none.",
        }
        schema["security"] = [{"bearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
