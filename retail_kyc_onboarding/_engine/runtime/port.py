"""RuntimePort: the only place ADK-version specifics live. The core talks to this interface; adapters implement it."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..datatypes import Principal

if TYPE_CHECKING:
    from ..engine.units import ContentUnit


SESSION_USER = "flow"  # ADK user id of every session: sessions are found by id, the requester lives in the run state


def app_name_for(export_id: str) -> str:
    """The ADK application name of an export: its id, made valid for ADK (a letter first, then letters, digits, ``_`` or ``-``; never ``user``).

    ADK keys sessions, ``app:`` state, artifacts and memory by application name. A name shared by every export would mix them
    when several exports use one session database, so the name is the export's own id.
    """
    name = re.sub(r"[^0-9a-zA-Z_]", "_", export_id)
    name = name if name[:1].isalpha() else f"x_{name}"
    return "x_user" if name == "user" else name


@dataclass
class PauseInfo:
    """A run waiting for an answer."""

    kind: Literal["input", "confirmation"]  # input: RequestInput; confirmation: ADK native tool confirmation
    interrupt_id: str  # id the answer must carry
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None  # set when this pause is a tool approval
    approval_record: dict[str, Any] | None = None


@dataclass
class ResumeInput:
    """The answer that continues a paused run: which interrupt it answers, its kind, and the response value."""
    interrupt_id: str
    kind: Literal["input", "confirmation"]
    response: Any


@dataclass
class UnitResult:
    """Outcome of running a unit: done, paused (with its PauseInfo) or error."""
    status: Literal["done", "paused", "error"]
    output: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    pause: PauseInfo | None = None
    error: str = ""
    session_id: str = ""


class RuntimePort(ABC):
    """Interface between the core and an ADK version or execution backend.

    An app name is derived per export so sessions of different exports do not mix.
    """
    def __init__(self, engine: Any):
        self.engine = engine
        self.app_name = app_name_for(engine.unit.manifest.export.id)  # per export, see app_name_for

    @abstractmethod
    async def run_unit(
        self,
        unit: ContentUnit,
        *,
        session_id: str,
        user_id: str,
        message: str | None,
        variables: dict[str, Any],
        principal: Principal,
        resume: ResumeInput | None = None,
        meta: dict[str, Any] | None = None,
    ) -> UnitResult:
        """Run (or continue) one unit in the given session. Returns when the unit finishes, pauses or fails."""

    @abstractmethod
    async def session_state(self, unit: ContentUnit, session_id: str, user_id: str) -> dict[str, Any] | None:
        """Current state of a session, or None if it does not exist."""

    @abstractmethod
    async def pending_pause(self, unit: ContentUnit, session_id: str, user_id: str) -> PauseInfo | None:
        """The pause a session is waiting on (found from the session itself, so it survives restarts)."""

    @abstractmethod
    async def pending_sessions(self, unit: ContentUnit, limit: int = 200) -> list[tuple[str, PauseInfo, dict[str, Any]]]:
        """Sessions that are waiting for an answer: (session id, pause, run info). A scan: meant for approver worklists."""
