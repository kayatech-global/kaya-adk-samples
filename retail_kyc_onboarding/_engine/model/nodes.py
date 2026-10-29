"""Typed views over Node.config for each node kind. Unknown keys are kept so newer exports load on older cores."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .graph import Node


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="allow")


class StructuredOutput(_Cfg):
    """Settings that make a node return JSON matching a schema, optionally stored as prefixed variables."""

    enabled: bool = False
    instructions: str = ""
    output_schema: dict[str, str] = Field(default_factory=dict)
    variable_prefix: str = ""


class InterruptCriteria(_Cfg):
    """The condition that decides whether an interrupt fires."""

    type: Literal["ALWAYS", "OUTPUT_CONTAINS", "OUTPUT_NOT_CONTAINS", "VARIABLE_CONDITION", "LLM_EVALUATION", "EXECUTION_COUNT"] = "ALWAYS"
    value: str | None = None  # OUTPUT_CONTAINS / OUTPUT_NOT_CONTAINS
    use_regex: bool = False
    variable_name: str | None = None  # VARIABLE_CONDITION: name, operator, threshold
    operator: Literal["==", "!=", "<", ">", ">=", "<="] | None = None
    threshold: str | None = None
    llm_prompt: str | None = None  # LLM_EVALUATION (later phase)
    count: int | None = None  # EXECUTION_COUNT: interrupt from the Nth run onward
    on_error: Literal["interrupt", "skip"] = "interrupt"


class InterruptSide(_Cfg):
    """One side (before or after a node) of an interrupt: whether it is on, its message and criteria."""

    enabled: bool = False
    message: str = ""
    option: str = ""
    criteria: InterruptCriteria | None = None  # None: interrupt every time when enabled
    actions: list[dict[str, str]] | None = None


class InterruptConfig(_Cfg):
    """Interrupt settings for the before and after side of a node."""

    before: InterruptSide = Field(default_factory=InterruptSide)
    after: InterruptSide = Field(default_factory=InterruptSide)


class ApprovalSettings(_Cfg):
    """Per-tool approval settings (artifact 17.7)."""

    policy: Literal["never", "always", "by_tool", "by_rule", "from_authz"] = "never"
    required: bool = False
    approvers: dict[str, Any] = Field(default_factory=dict)
    rules: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    on_timeout: Literal["deny", "escalate"] = "deny"


class AgentConfig(_Cfg):
    """Config of an agent node: prompt, model, tools, structured output and interrupts."""

    prompt: str = ""  # prompt entity id (resolved through the prompt factory)
    model: str = ""  # model alias / entity id
    tools: list[str] = Field(default_factory=list)
    task: str = ""  # plan step task text (sent as the user message)
    structured_output: StructuredOutput = Field(default_factory=StructuredOutput)
    interrupt: InterruptConfig = Field(default_factory=InterruptConfig)
    expose_output_as_variable: bool = False
    output_variable: str = ""
    guardrails: list[str] = Field(default_factory=list)


class ToolNodeConfig(_Cfg):
    """Config of a tool node: the tools to call and their request and output structures."""

    tools: list[str] = Field(default_factory=list)
    request_structures: dict[str, Any] = Field(default_factory=dict)  # JSON text or object, per tool
    output_structures: dict[str, Any] = Field(default_factory=dict)
    task: str = ""
    interrupt: InterruptConfig = Field(default_factory=InterruptConfig)


class SupervisorConfig(_Cfg):
    """Config of a supervisor node: how it picks the next node, with a turn limit and fallback."""

    prompt: str = ""
    model: str = ""
    max_turns: int = 3
    strategy: str = "llm_structured"
    strategy_settings: dict[str, Any] = Field(default_factory=dict)
    rules: list[dict[str, Any]] = Field(default_factory=list)  # for the 'rules' strategy
    fallback: str | None = None  # node id; default is end
    structured_output: StructuredOutput = Field(default_factory=StructuredOutput)


class FollowupConfig(_Cfg):
    """Config of a follow-up node: the message to ask, with optional answer validation."""

    message: str = ""
    task: str = ""
    validation: dict[str, Any] | None = None  # {task, agent, model, prompt}
    output_variable: str = ""
    agent: AgentConfig | None = None  # the plan worker that writes the question (Kaya runs it with the task)


class SubflowConfig(_Cfg):
    """Config of a sub-flow node: the bundled child graph, its input, and how its output returns."""

    child: str = ""  # child graph id, bundled under children/
    child_version: str = ""  # pinned at export
    input_message: str = ""
    initial_input: dict[str, Any] | str = Field(default_factory=dict)  # object or JSON text with {{Variable:x}} tokens
    output_structure: dict[str, Any] | str | None = None
    execution_mode: Literal["sync", "async"] = "sync"
    output_mode: Literal["all", "last", "custom", "none"] = "last"
    pass_current_state: bool = False
    timeout_seconds: float | None = 300


class BranchConfig(_Cfg):
    """Config of a routing-only node. ``excluded`` marks a source step that was not exported and is passed through."""

    excluded: bool = False
    source_function: str = ""  # the source node function of an excluded step


class IterationConfig(_Cfg):
    """Run a small body (one or more nodes, usually a Sub-IFLOW or an agent) once per item of a list."""

    array_input: str | list[Any] = ""  # variable reference ("{items}", "{{Variable:items}}") or JSON array text or a list
    item_variable_name: str = "current_item"
    index_variable_name: str = "current_index"
    body: list[Node] = Field(default_factory=list)  # run in order for each item
    parallel_execution: bool = False
    max_workers: int = 5
    continue_on_error: bool = True


class ExternalAgentConfig(_Cfg):
    """A partner agent reached over A2A (JSON-RPC message/send). Mirrors the fields of an external agent node."""

    url: str = ""
    timeout_ms: int = 30000
    polling_interval_ms: int = 5000
    poll_attempts: int = 10
    prompt_template: str = ""
    authorization: dict[str, Any] | None = None  # normalized authorization block (secrets by logical name)
    task: str = ""  # plan step task, appended to the prompt unless the template places {{Variable:kaya.message}}
    output_prefix: str = ""  # <prefix>.response, .status, .artifacts; Kaya uses the agent node's name
    retry_attempts: int = 0  # extra attempts after a failed call, with exponential backoff
    retry_min_wait: float = 1
    retry_max_wait: float = 30
