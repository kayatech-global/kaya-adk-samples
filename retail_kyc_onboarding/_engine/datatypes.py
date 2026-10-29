"""Plain data types that cross factory ports. No vendor types here, so ports stay replaceable."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of a run: who they are, their tenant, roles and claims."""

    subject: str
    tenant: str | None = None
    roles: tuple[str, ...] = ()
    claims: Mapping[str, Any] = field(default_factory=dict)
    token_ref: str | None = None  # a handle for the credentials port; never the token in traces

    def public(self) -> dict[str, Any]:
        """Return the subset of fields safe to expose in traces and responses (no claims, no token handle)."""
        return {"subject": self.subject, "tenant": self.tenant, "roles": list(self.roles)}

    def to_state(self) -> dict[str, Any]:
        """Serialize every field, including claims and the token handle, for storage in session state."""
        return {
            "subject": self.subject,
            "tenant": self.tenant,
            "roles": list(self.roles),
            "claims": dict(self.claims),
            "token_ref": self.token_ref,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any] | None) -> Principal:
        """Rebuild a principal from session state; empty or missing data yields ``ANONYMOUS``."""
        if not data:
            return ANONYMOUS
        return cls(
            data.get("subject", "anonymous"),
            data.get("tenant"),
            tuple(data.get("roles", ())),
            dict(data.get("claims", {})),
            data.get("token_ref"),
        )


ANONYMOUS = Principal(subject="anonymous")


@dataclass(frozen=True)
class Decision:
    """An authorization outcome: allow, deny, or require approval, with the reason and optional approvers."""

    effect: Literal["allow", "deny", "require_approval"]
    reason: str = ""
    obligations: tuple[str, ...] = ()
    approvers: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        # fail closed: every check blocks on "deny", so an effect spelt any other way ("DENY", "forbid") must not pass as allowed
        effect = str(self.effect).strip().lower()
        if effect not in ("allow", "deny", "require_approval"):
            object.__setattr__(self, "reason", f"unknown effect '{self.effect}': {self.reason}".rstrip(": "))
            effect = "deny"
        object.__setattr__(self, "effect", effect)


@dataclass(frozen=True)
class Credential:
    """Headers to attach to an outbound call, plus an optional expiry as epoch seconds."""

    headers: Mapping[str, str] = field(default_factory=dict)
    expires_at: float | None = None


@dataclass(frozen=True)
class CallContext:
    """Per-call data handed to tools, the Data Engine and credential providers."""

    principal: Principal
    run_id: str = ""
    session_id: str = ""
    node_id: str = ""
    idempotency_key: str = ""
    export_id: str = ""
    variables: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptSpec:
    """A resolved prompt: its text, version and content hash."""

    id: str
    version: str
    text: str
    meta: Mapping[str, Any] = field(default_factory=dict)
    sha256: str = ""


@dataclass(frozen=True)
class ModelSpec:
    """A model entity as exported: provider label, model name and sampling settings, with secrets held by logical name only."""

    id: str
    version: str = ""
    provider: str = ""  # provider label of the source flow: openai, azure, google, anthropic, bedrock, ...
    model_name: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    base_url: str | None = None
    api_key_secret: str | None = None  # logical secret name, never the key
    headers: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)
    default: bool = False  # the step named no model: this is the export's default model, which FLOW_DEFAULT_MODEL may replace
    fallbacks: tuple[str, ...] = ()  # model ids (or provider/name) tried in order when this one is overloaded or down
    sha256: str = ""  # content digest computed by the exporter; a mismatch marks the model as overridden


@dataclass(frozen=True)
class ToolDef:
    """A tool entity as exported: its type (``api`` or ``mcp``), parameters, config and approval settings."""

    name: str
    type: str  # api | mcp
    description: str = ""
    parameters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    optional_parameters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)
    version: str = ""
    manual_execution: bool = False
    approval: Mapping[str, Any] = field(default_factory=dict)
    sha256: str = ""  # content digest computed by the exporter; a mismatch marks the tool as overridden

    def with_timeout(self, seconds: float) -> ToolDef:
        """A copy whose calls time out after ``seconds``."""
        return replace(self, config={**self.config, "timeout": seconds})

    def with_approval(self, **approval: Any) -> ToolDef:
        """A copy with these approval settings merged in (``policy``, ``required``, ``approvers``, ``timeout_seconds``)."""
        return replace(self, approval={**self.approval, **approval})

    def with_authorization(self, authorization: Mapping[str, Any]) -> ToolDef:
        """A copy with another normalized ``authorization`` block (secrets by logical name only)."""
        return replace(self, config={**self.config, "authorization": dict(authorization)})


@dataclass
class ToolResult:
    """The outcome of a tool call: success flag, text and structured data, or an error message."""

    ok: bool
    text: str = ""
    data: Any = None
    status: int | None = None
    error: str = ""

    def as_text(self) -> str:
        """Return the text on success, else the error message (falling back to the text)."""
        return self.text if self.ok else (self.error or self.text)


@dataclass
class JobResult:
    """The state of a Data Engine job, including files, response and error payloads."""

    job_id: str
    status: str
    timed_out: bool = False
    response: Any = None
    files: list[Any] | None = None
    error: Any = None

    @property
    def is_error(self) -> bool:
        """True when the job timed out or ended in a failed, cancelled, expired or partially failed status."""
        return self.timed_out or self.status in {"FAILED", "CANCELLED", "EXPIRED", "COMPLETED_WITH_ERRORS"}


@dataclass(frozen=True)
class RouteChoice:
    """A router decision: the next node id, or ``None`` to finish, with the reason."""

    next_node: str | None  # None means finish
    reason: str = ""


@dataclass(frozen=True)
class ApprovalRequirement:
    """Whether a tool call needs human approval, who may approve it and what happens on timeout."""

    required: bool
    approvers: Mapping[str, Any] = field(default_factory=dict)
    rules: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None
    on_timeout: str = "deny"
    reason: str = ""
