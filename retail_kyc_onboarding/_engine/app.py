"""What a client changes about an export without editing its content: per-entity overrides.

Precedence, highest first: arguments to ``build_workflow(...)``, the export's ``settings.py``, the exported workflow, the app default.
An override names an entity by its id; an unknown id is an error that lists the known ones, so a typo never passes silently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .datatypes import ModelSpec, PromptSpec, ToolDef
from .factories.adk_plugin import AdkPluginFactory, NoAdkPlugins
from .factories.api import ApiFactory, HttpApiFactory
from .factories.approval import (
    AdkRequestInputChannel,
    ApprovalChannel,
    ApprovalPolicy,
    ApproverResolver,
    ByToolPolicy,
    StandardApprovers,
)
from .factories.base import Provider
from .factories.mcp import McpFactory
from .factories.mcp_client import McpClientFactory
from .factories.misc import (
    GuardrailFactory,
    NoGuardrails,
    NoopRegistration,
    NoRetrieval,
    RegistrationFactory,
    RetrievalFactory,
    RunStoreFactory,
    UriRunStore,
)
from .factories.model import LiteLlmModels, ModelFactory
from .factories.prompt import ContentPrompts, PromptFactory
from .factories.router import LlmStructuredRouter, RouterStrategy
from .factories.secret import EnvSecrets, SecretFactory
from .factories.security import AllowAllAuthz, AnonymousAuthn, Authenticator, Authorizer, CredentialProvider, NoCredentials
from .factories.session import SessionFactory, UriSessions
from .factories.telemetry import ConsoleTelemetry, TelemetryFactory

if TYPE_CHECKING:
    from google.adk.models.base_llm import BaseLlm

    from .factories.api import ToolHandle


@dataclass(frozen=True)
class Overrides:
    """Replacements for single entities of one content unit, and of its children by child id.

    * ``models``: a ``ModelSpec`` (built by the model provider) or a ready ADK ``BaseLlm`` (used as is, ADK style).
    * ``prompts``: new text (the exported version is kept) or a whole ``Prompt``.
    * ``tools``: a ``ToolDef`` (built by the api or mcp provider) or a ready ``ToolHandle``. Either way every call passes the guard.
    """

    models: Mapping[str, ModelSpec | BaseLlm] = field(default_factory=dict)
    prompts: Mapping[str, str | PromptSpec] = field(default_factory=dict)
    tools: Mapping[str, ToolDef | ToolHandle] = field(default_factory=dict)
    children: Mapping[str, Overrides] = field(default_factory=dict)

    def merged(self, higher: Overrides | None) -> Overrides:
        """These overrides with ``higher`` applied on top: an entity ``higher`` names wins."""
        if higher is None:
            return self
        kids = {k: self.children.get(k, Overrides()).merged(v) for k, v in higher.children.items()}
        return Overrides(
            models={**self.models, **higher.models},
            prompts={**self.prompts, **higher.prompts},
            tools={**self.tools, **higher.tools},
            children={**self.children, **kids},
        )

    def __bool__(self) -> bool:
        return bool(self.models or self.prompts or self.tools or self.children)


def as_overrides(value: Overrides | Mapping[str, Any] | None) -> Overrides:
    """``value`` as ``Overrides``; a mapping uses the field names as keys."""
    if value is None:
        return Overrides()
    if isinstance(value, Overrides):
        return value
    return Overrides(**dict(value))


class FlowSettings(BaseSettings):
    """The settings of an export, read from ``FLOW_`` environment variables. Every field has a default.

    ADK's own variables (``GOOGLE_API_KEY``, ``GOOGLE_GENAI_USE_ENTERPRISE``, ...) are read by ADK, not here. An export
    subclasses this as ``Settings`` in its ``settings.py`` to read the ``.env`` beside the package as well.
    """

    model_config = SettingsConfigDict(env_prefix="FLOW_", extra="ignore")

    profile: Literal["development", "production"] = "development"
    conditions_mode: Literal["strict", "compat"] = "strict"  # compat reproduces the source platform's expression quirks
    fail_closed: bool | None = None  # None: closed unless the profile is development
    session_uri: str = (
        "memory://"  # what `adk web --session_service_uri` takes: memory://, sqlite+aiosqlite:///runs.db, postgresql+asyncpg://...
    )
    default_model: str = ""  # provider/name for every step that named no model; empty keeps the export's default
    principal_from: Literal["fixed", "state", "user_id"] = "fixed"  # who a flow_agent run is for
    fixed_subject: str = "adk-agent"
    allow_user_id: bool = False  # principal_from user_id in production: the embedding platform vouches for the user
    host: str = "127.0.0.1"
    port: int = 8080
    provider_mode: str = ""  # the runtime.yaml profile to run with (default, arc, ...); empty takes the file's `mode`
    runtime_file: str = "runtime.yaml"  # the provider profiles, relative to the package folder

    @property
    def guard_fail_closed(self) -> bool:
        """Whether guard checks fail closed: ``fail_closed`` when set, otherwise closed unless the profile is development."""
        return self.fail_closed if self.fail_closed is not None else self.profile != "development"


@dataclass
class Providers:
    """One provider per factory kind, built in code. ``None`` takes the default provider for that kind."""

    secret: SecretFactory | None = None
    credentials: CredentialProvider | None = None
    authn: Authenticator | None = None
    authz: Authorizer | None = None
    prompt: PromptFactory | None = None
    model: ModelFactory | None = None
    api: ApiFactory | None = None
    mcp: McpFactory | None = None
    session: SessionFactory | None = None
    telemetry: TelemetryFactory | None = None
    adk_plugin: AdkPluginFactory | None = None
    approval_policy: ApprovalPolicy | None = None
    approver_resolver: ApproverResolver | None = None
    approval_channel: ApprovalChannel | None = None
    router: RouterStrategy | None = None
    runstore: RunStoreFactory | None = None
    registration: RegistrationFactory | None = None
    guardrail: GuardrailFactory | None = None
    retrieval: RetrievalFactory | None = None
    mode: str = ""  # the runtime.yaml profile these providers came from, for the trace; not a factory kind
    overrides: Overrides = field(default_factory=Overrides)  # entity overrides the profile's `content:` asks for; not a factory kind

    def given(self) -> dict[str, Provider]:
        """The providers that are set, by kind."""
        return {f.name: v for f in fields(self) if isinstance(v := getattr(self, f.name), Provider)}


def default_providers(settings: FlowSettings | None = None) -> Providers:
    """The providers an export runs with when nothing else is chosen: environment secrets, the export's own prompts, LiteLLM
    models, HTTP and MCP tools, sessions and run records at ``settings.session_uri``, and the demo identity defaults (anonymous
    callers, everything allowed) that ``profile: production`` refuses."""
    s = settings or FlowSettings()
    return Providers(
        secret=EnvSecrets(),
        credentials=NoCredentials(),
        authn=AnonymousAuthn(),
        authz=AllowAllAuthz(),
        prompt=ContentPrompts(),
        model=LiteLlmModels({"default_model": s.default_model}),
        api=HttpApiFactory(),
        mcp=McpClientFactory(),
        session=UriSessions({"uri": s.session_uri}),
        telemetry=ConsoleTelemetry(),
        adk_plugin=NoAdkPlugins(),
        approval_policy=ByToolPolicy(),
        approver_resolver=StandardApprovers(),
        approval_channel=AdkRequestInputChannel(),
        router=LlmStructuredRouter(),
        runstore=UriRunStore({"uri": s.session_uri}),
        registration=NoopRegistration(),
        guardrail=NoGuardrails(),
        retrieval=NoRetrieval(),
    )


@dataclass
class AppConfig:
    """Everything an engine is started with besides its content: settings, providers and overrides."""

    settings: FlowSettings = field(default_factory=FlowSettings)
    providers: Providers | None = None  # None: default_providers(settings)
    overrides: Overrides = field(default_factory=Overrides)
