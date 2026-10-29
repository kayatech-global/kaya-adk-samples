"""Retail KYC Onboarding as a Google ADK agent.

`adk run retail_kyc_onboarding`, `adk web` and `adk api_server` load this module and read `app` (or `root_agent`):

* `root_agent`: an ADK `Workflow` built from the exported Kaya graph (`workflow/graph.py`); each step is a short chain of
  ADK nodes (its phases and pauses).
* `app`: an ADK `App` around it, resumable, so a run paused for input or an approval continues where it stopped.

The workflow comes from `workflow/`; providers and replacements from `settings.py`; nothing here needs editing.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any

from ._engine.runtime.entry import build_engine, build_workflow_and_app, run_blocking
from .workflow import WORKFLOW
from .settings import build_config

if TYPE_CHECKING:
    from google.adk.apps import App
    from google.adk.workflow import Workflow

    from ._engine.engine.engine import Engine

    root_agent: Workflow
    app: App


async def abuild(**overrides: Any) -> tuple[Workflow, App]:
    """A new ``(root_agent, app)`` pair; takes the keyword arguments of ``settings.build_config`` (models, prompts, ...)."""
    workflow, app = await build_workflow_and_app(WORKFLOW, build_config(**overrides))
    return workflow, app


def build(**overrides: Any) -> tuple[Workflow, App]:
    """``abuild`` for synchronous code (inside a running event loop it builds on a worker thread)."""
    return run_blocking(lambda: abuild(**overrides))


async def abuild_engine(*, extra_sinks: list[Any] | None = None, **overrides: Any) -> Engine:
    """A started engine, what the governed run host serves (docs/INTEGRATION.md, "Governed run host")."""
    return await build_engine(WORKFLOW, build_config(**overrides), extra_sinks=extra_sinks)


@cache
def _built() -> tuple[Workflow, App]:
    return build()


def __getattr__(name: str) -> Any:
    # `root_agent` and `app` are built on first read, not during the import: `adk run` and `adk web` import this module from
    # inside their own event loop, and building there (on a worker thread) would deadlock on the import lock
    if name == "root_agent":
        return _built()[0]
    if name == "app":
        return _built()[1]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
