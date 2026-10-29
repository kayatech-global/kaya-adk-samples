"""Request and response models of the run host. They are the contract in /openapi.json, so they carry descriptions and examples."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Problem(BaseModel):
    """An error, as RFC 9457 `application/problem+json` (the `detail` member is what earlier clients read)."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"type": "about:blank", "title": "Forbidden", "status": 403, "detail": "run.start denied"}}
    )
    type: str = Field("about:blank", description="A URI that identifies the problem type; `about:blank` means the status code says it all.")
    title: str = Field(description="Short, human-readable summary of the problem type.")
    status: int = Field(description="The HTTP status code.")
    detail: str = Field("", description="Explanation specific to this occurrence.")
    instance: str | None = Field(None, description="The request path.")


class RunRequest(BaseModel):
    """Start a run."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"message": "My brakes grind when I stop.", "variables": {"customer_id": "C-1001"}}}
    )
    message: str = Field(description="The user's message that starts the run.")
    session_id: str | None = Field(None, description="Continue an existing session; omitted starts a new one.")
    variables: dict[str, Any] = Field(default_factory=dict, description="Initial values for workflow variables.")
    stream: bool = Field(
        False, description="Return a server-sent event stream of trace records and the result (same as `Accept: text/event-stream`)."
    )


class ResumeRequest(BaseModel):
    """Answer a paused run: `text` for a question, `approved` (with an optional `comment`) for an approval."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"text": "C-1001"}, {"approved": True, "comment": "checked with ops"}]})
    text: str | None = Field(None, description="The answer to a question.")
    approved: bool | None = Field(None, description="True approves, False rejects.")
    comment: str = Field("", description="Recorded with the approval decision.")
    interrupt_id: str | None = Field(None, description="Which pause this answers, when more than one is open.")


class PauseView(BaseModel):
    """What a paused run is waiting for."""

    kind: str = Field(description="`input` (a question) or `approval`.")
    interrupt_id: str
    message: str = Field(description="What is being asked.")
    payload: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = Field(None, description="Present for approvals: the id to answer at `/v1/approvals/{approval_id}`.")
    approvers: dict[str, Any] = Field(
        default_factory=dict,
        description="Who may answer, as resolved by the approver provider (for example `{kind: roles, any_of: [...]}`).",
    )


class RunResult(BaseModel):
    """The state of a run after a start or a resume."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "done",
                "session_id": "6b0f...",
                "run_id": "a1c2...",
                "output": "Your estimate is ...",
                "pause": None,
                "variables": {},
                "error": "",
            }
        }
    )
    status: Literal["done", "paused", "error"]
    session_id: str
    run_id: str = ""
    output: str = Field("", description="The final (or latest) answer.")
    pause: PauseView | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class RunStatus(BaseModel):
    """A session at rest."""

    session_id: str
    run_id: str | None = None
    requested_by: str = Field(description="The principal that started the run.")
    status: Literal["idle", "paused"]
    last_status: Literal["done", "paused", "error"] = Field(default="done", description="How the session's last run ended.")
    last_error: str = Field(default="", description="Why the last run failed, when it did.")
    updated_at: str | None = Field(default=None, description="When the last run ended (ISO 8601, UTC).")
    pause: PauseView | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    last_output: str = ""


class TraceRecords(BaseModel):
    """Recorded decisions of one run, oldest first."""

    run_id: str
    records: list[dict[str, Any]] = Field(
        description="One object per event: `step.start`, `model.call`, `tool.call`, `route`, `approval.*`, `authz.*`, ..."
    )


class ApprovalItem(PauseView):
    """A pending approval as listed to approvers, with the session and run it belongs to."""

    session_id: str
    run_id: str = ""
    requested_by: str = ""


class Health(BaseModel):
    """Liveness report: overall status and the providers that are unhealthy."""

    status: Literal["ok", "degraded"] = Field(description="`degraded` means a provider this export needs is unhealthy.")
    export: dict[str, Any]
    unhealthy: dict[str, str] = Field(default_factory=dict, description="Provider kind to its health message.")


class Ready(BaseModel):
    """Readiness report: whether the export is loaded and can accept runs."""

    ready: bool
    export: dict[str, Any] = Field(default_factory=dict)


class ExportInfo(BaseModel):
    """Metadata of the loaded export: identity, source, target, entities, children and exclusions."""

    export: dict[str, Any]
    source: dict[str, Any]
    target: dict[str, Any]
    entities: dict[str, Any]
    children: dict[str, str]
    exclusions: list[str]
    modified_entities: list[str] = Field(description="Entities that differ from the exported content: `prompt:<id> (edited)`, `model:<id> (override)`.")
    providers: list[dict[str, str]] = Field(
        description="The provider chosen for each factory kind: `kind`, `provider` and the implementing `class`."
    )
    core: str = Field(description="Engine version.")
