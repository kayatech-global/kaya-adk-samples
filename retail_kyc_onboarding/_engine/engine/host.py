"""RunHost: the entry used by the CLI, the HTTP servers and tests. Starts runs, resumes paused ones, verifies approvers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..datatypes import Principal
from ..errors import PolicyDenied, RunError, RunNotFound
from ..runtime.port import SESSION_USER, PauseInfo, ResumeInput, UnitResult
from ..trace.context import carrier_from, use_carrier
from .engine import Engine
from .state import RunState

OUTCOME = "outcome:"  # runstore key prefix: how a session's last run ended
TRACE = "trace:"  # runstore key prefix: the W3C trace context the session was started in


@dataclass
class PauseView:
    """Client-safe view of a paused run, either a question (input) or an approval."""

    kind: str  # "input" | "approval"
    interrupt_id: str
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None
    approvers: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for the HTTP and CLI layers."""
        return {
            "kind": self.kind,
            "interrupt_id": self.interrupt_id,
            "message": self.message,
            "payload": self.payload,
            "approval_id": self.approval_id,
            "approvers": self.approvers,
        }


@dataclass
class HostResult:
    """Outcome of starting or resuming a run, as returned to a client."""

    status: str  # done | paused | error
    session_id: str
    output: str = ""
    pause: PauseView | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for the HTTP and CLI layers."""
        return {
            "status": self.status,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "output": self.output,
            "pause": self.pause.to_dict() if self.pause else None,
            "variables": self.variables,
            "error": self.error,
        }


@dataclass
class Answer:
    """An answer to a pause. For approvals set ``approved``; for questions set ``text``."""

    text: str | None = None
    approved: bool | None = None
    comment: str = ""
    interrupt_id: str | None = None


@dataclass
class PendingItem:
    """One paused run in a worklist, with who asked and which run it belongs to."""

    session_id: str
    pause: PauseView
    requested_by: str
    run_id: str


class RunHost:
    """Entry point for starting runs, resuming paused ones and listing what is waiting on a person."""

    def __init__(self, engine: Engine):
        self.engine = engine
        engine.host = self

    @staticmethod
    def _view(pause: PauseInfo | None) -> PauseView | None:
        if pause is None:
            return None
        record = pause.approval_record or {}
        is_approval = pause.approval_id is not None
        # the await token is the system's credential for waking the run; no client ever sees it
        visible = {k: v for k, v in pause.payload.items() if k != "await_token"}
        return PauseView(
            "approval" if is_approval else "input",
            pause.interrupt_id,
            pause.message,
            visible,
            pause.approval_id,
            dict((record.get("requirement") or {}).get("approvers") or {}),
        )

    async def _recorded(self, result: HostResult) -> HostResult:
        """Keep how the session's last run ended, so a later status read can tell a failed run from a finished one."""
        await self.engine.factories.runstore.put(
            OUTCOME + result.session_id,
            {
                "status": result.status,
                "run_id": result.run_id,
                "error": result.error if result.status == "error" else "",
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
        return result

    @staticmethod
    def _result(res: UnitResult, session_id: str, run_id: str) -> HostResult:
        return HostResult(res.status, session_id, res.output, RunHost._view(res.pause), res.variables, res.error, run_id)

    async def start(
        self,
        *,
        message: str,
        principal: Principal,
        session_id: str | None = None,
        variables: dict[str, Any] | None = None,
        run_id: str | None = None,
        trace: Mapping[str, Any] | None = None,
    ) -> HostResult:
        """Start a run for a session, creating the session id and run id when absent.

        ``trace`` holds the caller's ``traceparent`` and ``tracestate`` (request headers will do); the run joins that trace.

        Raises:
            RunError: The session is already waiting for an answer.
            PolicyDenied: The session belongs to another user.
        """
        carrier = carrier_from(trace)
        with use_carrier(carrier):
            return await self._start(message, principal, session_id, variables, run_id, carrier)

    async def _start(
        self,
        message: str,
        principal: Principal,
        session_id: str | None,
        variables: dict[str, Any] | None,
        run_id: str | None,
        carrier: dict[str, str],
    ) -> HostResult:
        engine = self.engine
        session_id = session_id or str(uuid.uuid4())
        run_id = run_id or str(uuid.uuid4())
        tracer = engine.tracer.bind(session=session_id, run_id=run_id)
        decision = await engine.guard.check(
            principal,
            "run.start",
            {"id": engine.unit.manifest.export.id, "version": engine.unit.manifest.export.version},
            tracer=tracer,
        )
        if decision.effect == "deny":
            return HostResult("error", session_id, error=f"Denied: run.start ({decision.reason})", run_id=run_id)
        existing = await engine.runtime.pending_pause(engine.unit, session_id, SESSION_USER)
        if existing is not None:
            raise RunError("this session is waiting for an answer; resume it instead of sending a new message")
        state = await engine.runtime.session_state(engine.unit, session_id, SESSION_USER)
        if state is not None:
            owner = RunState(state).principal()
            if owner.subject != principal.subject:
                raise PolicyDenied("run.start", session_id, "the session belongs to another user")
        if carrier:
            await engine.factories.runstore.put(TRACE + session_id, carrier)
        res = await engine.runtime.run_unit(
            engine.unit,
            session_id=session_id,
            user_id=SESSION_USER,
            message=message,
            variables=variables or {},
            principal=principal,
            meta={"run_id": run_id},
        )
        return await self._recorded(self._result(res, session_id, run_id))

    async def _can_read(self, state: RunState, principal: Principal, session_id: str) -> None:
        if state.principal().subject == principal.subject:
            return
        decision = await self.engine.guard.check(principal, "run.read", {"id": session_id})
        if decision.effect == "deny":
            raise PolicyDenied("run.read", session_id, decision.reason or "not the requester")

    async def status(self, session_id: str, principal: Principal) -> dict[str, Any]:
        """Report a session's state, pause and variables.

        Raises:
            RunError: The session is unknown.
            PolicyDenied: The caller is not the requester and may not read the run.
        """
        raw = await self.engine.runtime.session_state(self.engine.unit, session_id, SESSION_USER)
        if raw is None:
            raise RunNotFound("unknown session")
        state = RunState(raw)
        await self._can_read(state, principal, session_id)
        pause = await self.engine.runtime.pending_pause(self.engine.unit, session_id, SESSION_USER)
        run = state.run()
        outcome = await self.engine.factories.runstore.get(OUTCOME + session_id) or {}
        return {
            "session_id": session_id,
            "run_id": run.get("run_id"),
            "requested_by": state.principal().subject,
            "status": "paused" if pause else "idle",
            "last_status": "paused" if pause else outcome.get("status", "done"),
            "last_error": outcome.get("error", "") if not pause else "",
            "updated_at": outcome.get("at"),
            "pause": (view.to_dict() if (view := self._view(pause)) else None),
            "variables": state.variables(),
            "last_output": state.last(),
        }

    async def pending(self, session_id: str, principal: Principal) -> PauseView | None:
        """Return the pause the session is waiting on, or None."""
        pause = await self.engine.runtime.pending_pause(self.engine.unit, session_id, SESSION_USER)
        return self._view(pause)

    async def worklist(self, principal: Principal) -> list[PendingItem]:
        """Pending pauses the principal may answer: their own questions, and approvals they are allowed to give."""
        items: list[PendingItem] = []
        for session_id, pause, run in await self.engine.runtime.pending_sessions(self.engine.unit):
            requester = Principal.from_state(run.get("principal"))
            view = self._view(pause)
            if view is None or pause.payload.get("internal"):  # a background wait: only the system completes it
                continue
            if view.kind == "approval":
                record = pause.approval_record
                if record is None:
                    continue
                ok, _ = await self.engine.approvals.resolver.may_answer(self.engine.approvals.requirement_of(record), principal, requester)
                if not ok:
                    continue
            elif requester.subject != principal.subject:
                continue
            items.append(PendingItem(session_id, view, requester.subject, str(run.get("run_id", ""))))
        return items

    async def find_approval(self, approval_id: str) -> str | None:
        """Return the session id waiting on this approval, or None."""
        for session_id, pause, _ in await self.engine.runtime.pending_sessions(self.engine.unit):
            if pause.approval_id == approval_id:
                return session_id
        return None

    async def _carrier(self, session_id: str, trace: Mapping[str, Any] | None) -> dict[str, str]:
        """The caller's trace context when it sent one, else the one the session was started in."""
        return carrier_from(trace) or dict(await self.engine.factories.runstore.get(TRACE + session_id) or {})

    async def resume(
        self,
        *,
        session_id: str,
        answer: Answer,
        answerer: Principal,
        owner: str | None = None,
        trace: Mapping[str, Any] | None = None,
    ) -> HostResult:
        """Answer a paused run. ``owner`` is accepted for compatibility and ignored: sessions are found by id.

        The run continues the caller's trace (``trace``) or, without one, the trace it was started in.
        """
        with use_carrier(await self._carrier(session_id, trace)):
            return await self._resume(session_id, answer, answerer)  # ``owner`` is ignored

    async def _resume(self, session_id: str, answer: Answer, answerer: Principal) -> HostResult:
        engine = self.engine
        runtime = engine.runtime
        pause = await runtime.pending_pause(engine.unit, session_id, SESSION_USER)
        if pause is None:
            raise RunError("this session is not waiting for an answer")
        if pause.payload.get("internal"):
            raise RunError("this run is waiting for background work; only the system can complete it, not an answer")
        if answer.interrupt_id and answer.interrupt_id != pause.interrupt_id:
            raise RunError(f"the session is waiting on '{pause.interrupt_id}', not '{answer.interrupt_id}'")
        state = RunState(await runtime.session_state(engine.unit, session_id, SESSION_USER) or {})
        requester = state.principal()
        run_id = str(state.run().get("run_id", ""))
        tracer = engine.tracer.bind(session=session_id, run_id=run_id)
        if pause.approval_id:
            record = pause.approval_record or state.pending().get(pause.approval_id)
            if record is None:
                raise RunError("the approval request is no longer pending")
            ok, why = await engine.approvals.verify_answer(record, answerer, tracer)
            if not ok:
                raise PolicyDenied("approval.answer", pause.approval_id, why)
            if answer.approved is None:
                raise RunError("an approval is answered with approved true or false, not with text")
            approved = bool(answer.approved)
            base = {"approved": approved, "approver": answerer.public(), "comment": answer.comment, "approval_id": pause.approval_id}
            response: Any = {"confirmed": approved, "payload": base} if pause.kind == "confirmation" else base
        else:
            if answerer.subject != requester.subject:
                decision = await engine.guard.check(answerer, "run.resume", {"id": session_id}, tracer=tracer)
                if decision.effect == "deny":
                    raise PolicyDenied("run.resume", session_id, decision.reason or "only the requester can answer this question")
            response = answer.text if answer.text is not None else str(answer.approved)
            tracer.emit("run.answer", interrupt_id=pause.interrupt_id, answerer=answerer.public(), requester=requester.public())
        res = await runtime.run_unit(
            engine.unit,
            session_id=session_id,
            user_id=SESSION_USER,
            message=None,
            variables={},
            principal=requester,
            resume=ResumeInput(pause.interrupt_id, pause.kind, response),
            meta={"run_id": run_id},
        )
        return await self._recorded(self._result(res, session_id, run_id))

    async def deliver(self, *, session_id: str, token: str, result: dict[str, Any]) -> HostResult:
        """Wake a run that is waiting for background work (an asynchronous child).

        Only the system calls this, with the token the waiting phase issued. It is not an answer from a person, so there is no answerer check;
        the token is the credential, and a pause that is not waiting for this token is left alone.
        """
        engine = self.engine
        pause = await engine.runtime.pending_pause(engine.unit, session_id, SESSION_USER)
        if pause is None or pause.payload.get("await_token") != token:
            raise RunError("the run is not waiting for this result")
        state = RunState(await engine.runtime.session_state(engine.unit, session_id, SESSION_USER) or {})
        run_id = str(state.run().get("run_id", ""))
        await engine.factories.runstore.put(
            f"delivered:{token}", dict(result)
        )  # the wake-up reads its data from here, never from the reply
        engine.tracer.bind(session=session_id, run_id=run_id).emit("run.delivered", token=token[:8], status=result.get("status"))
        with use_carrier(await self._carrier(session_id, None)):  # background work continues the run's own trace
            res = await engine.runtime.run_unit(
                engine.unit,
                session_id=session_id,
                user_id=SESSION_USER,
                message=None,
                variables={},
                principal=state.principal(),
                resume=ResumeInput(pause.interrupt_id, pause.kind, {"await_token": token}),
                meta={"run_id": run_id},
            )
        return await self._recorded(self._result(res, session_id, run_id))
