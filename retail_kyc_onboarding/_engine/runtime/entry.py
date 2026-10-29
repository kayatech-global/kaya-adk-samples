"""What an export's ``agent.py`` builds for ADK: the ``Workflow`` (``root_agent``), its ``App``, and the run-host agent.

`adk web`, `adk run` and `adk api_server` import the package and read ``root_agent`` or ``app``. That path runs the workflow on
ADK's own runner and session service, with the client's ADK plugins but without the run host (no trace plugin, no
authentication); production runs use the run host (``server.py``) or ``flow_agent``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..app import AppConfig
from ..content import ExportContent
from ..engine.engine import Engine
from .port import app_name_for

T = TypeVar("T")


def run_blocking(make: Callable[[], Awaitable[T]]) -> T:
    """Run the coroutine ``make()`` to completion from synchronous code.

    ADK's agent loader reads ``root_agent`` from inside its own event loop, so when a loop is already running the coroutine
    runs on a fresh loop in a worker thread instead of failing.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make())  # type: ignore[arg-type]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="flow-build") as pool:
        return pool.submit(lambda: asyncio.run(make())).result()  # type: ignore[arg-type]


async def build_engine(content: ExportContent, config: AppConfig, *, extra_sinks: list[Any] | None = None) -> Engine:
    """A started engine for an export's content and config."""
    return await Engine.from_content(content, config, extra_sinks=extra_sinks)


async def build_workflow_and_app(content: ExportContent, config: AppConfig) -> tuple[Any, Any]:
    """``(workflow, app)``: the ADK ``Workflow`` and an ADK ``App`` around it with resumability on and the client's plugins.

    A paused run can only be continued from the same invocation when the app is resumable, so `adk web` gets the same pause and
    resume behaviour as the run host.
    """
    from google.adk.apps.app import App

    from .adk_v2.adapter import resumability

    engine = await build_engine(content, config)
    workflow = await engine.runtime.workflow(engine.unit)
    app = App(
        name=app_name_for(content.identity.export.id),
        root_agent=workflow,
        plugins=engine.adk_plugins,
        resumability_config=resumability(),
    )
    return workflow, app


def flow_agent_for(build: Callable[[], Awaitable[Engine]], *, name: str = "flow_agent", settings: Any = None) -> Any:
    """The export as one ADK agent (``ExecutionAgent``) whose every turn is a run-host call. The engine is built on first use."""
    from .execution_agent import ExecutionAgent

    return ExecutionAgent.for_builder(build, name=name, settings=settings)
