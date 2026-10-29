"""Engine: one loaded export with its factories, guard, tracer and per-unit caches."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from google.adk.models.base_llm import BaseLlm
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from ..app import AppConfig, FlowSettings, Overrides, default_providers
from ..content import ExportContent
from ..datatypes import ModelSpec, PromptSpec
from ..errors import ConfigError, CoreVersionError
from ..factories import BuildContext, FactorySet
from ..factories.api import ToolHandle
from ..factories.base import Provider
from ..factories.model import resolve_model
from ..model import Node, NodeKind
from ..trace import Tracer
from ..version import CORE_VERSION
from .approvals import ApprovalService
from .evaluator import build_route_function
from .guard import Guard
from .plan import NodePlan
from .units import ContentUnit

log = logging.getLogger("flow.engine")


def select_adk_runtime(engine: Engine) -> Any:
    """The Google ADK runtime: the ``Workflow`` adapter."""
    from ..runtime.adk_v2.adapter import AdkV2Runtime

    return AdkV2Runtime(engine)


def iteration_body(node: Node) -> list[Node]:
    """The nodes an iteration node runs for each item (empty for any other node)."""
    if node.kind != NodeKind.ITERATION:
        return []
    return [Node.model_validate(n) for n in node.config.get("body") or []]


class Engine:
    """One loaded export with its factories, guard, tracer and per-unit caches of models, tools, route functions and plans."""
    def __init__(self, unit: ContentUnit, settings: FlowSettings, factories: FactorySet, tracer: Tracer):
        self.unit, self.settings, self.factories, self.tracer = unit, settings, factories, tracer
        self.guard = Guard(factories.authz, tracer, settings.guard_fail_closed)
        self.approvals = ApprovalService(factories.approval_policy, factories.approver_resolver, factories.approval_channel, self.guard)
        self.conditions_mode = settings.conditions_mode
        self._models: dict[tuple[str, str], BaseLlm] = {}
        self._tools: dict[tuple[str, str], ToolHandle] = {}
        self._route_fns: dict[tuple[str, str], Callable[..., Any]] = {}
        self._plans: dict[tuple[str, str], NodePlan] = {}
        self.runtime: Any = None  # RuntimePort, set by Engine.start
        self.adk_plugins: list[Any] = []  # the client's ADK plugins (factory kind adk_plugin), set by Engine.start
        self.required_kinds: set[str] = set()  # provider kinds this export cannot run without
        self.provider_mode = "default"  # the runtime.yaml profile the providers came from
        self.host: Any = None  # the RunHost serving this engine (set by RunHost), used to wake paused runs when background work finishes
        self._tasks: set[Any] = set()
        self.agent_index: dict[str, tuple[ContentUnit, Node]] = {}  # ADK agent name -> node (for trace plugin)

    # ------------------------------------------------------------------ loading

    @classmethod
    async def from_content(
        cls,
        content: ExportContent | ContentUnit,
        config: AppConfig | None = None,
        *,
        overrides: Overrides | None = None,
        providers: Mapping[str, Provider] | None = None,
        env: Mapping[str, str] | None = None,
        extra_sinks: list[Any] | None = None,
        base_dir: Path | str | None = None,
    ) -> Engine:
        """Build and start an engine for typed content.

        ``config`` holds the settings, the providers built in code (unset kinds take the defaults) and overrides; ``None`` is
        ``AppConfig()``. ``overrides`` replace single prompts, models and tools on top of the config's; ``providers`` replace
        whole factory kinds; ``base_dir`` is where relative file settings of providers (a rules file, a trace file) are
        resolved, the current folder by default.

        Raises:
            CoreVersionError: The export needs a different engine version.
            ConfigError: core_requires is not a valid version range, an override names an unknown entity, or a provider is of
                the wrong kind or already serves another engine.
        """
        config = config if config is not None else AppConfig()
        settings = config.settings
        unit = content if isinstance(content, ContentUnit) else ContentUnit.from_content(content, config.overrides.merged(overrides))
        requires = unit.manifest.core_requires
        if requires:
            try:
                if CORE_VERSION not in SpecifierSet(requires, prereleases=True):
                    raise CoreVersionError(f"this export needs engine version '{requires}', this engine is {CORE_VERSION}")
            except InvalidSpecifier as exc:
                raise ConfigError(f"core_requires '{requires}' is not a valid version range") from exc
        ctx = BuildContext(
            export_root=Path(base_dir).resolve() if base_dir is not None else Path.cwd(),
            env=dict(env) if env is not None else _env(),
            manifest=unit.manifest,
            units={u.scope: u.manifest for u in unit.all_units() if u.scope},
            content_units={u.scope: u for u in unit.all_units()},
            profile=settings.profile,
        )
        from ..factories import builtin  # noqa: F401  (registers built-in providers)

        chosen = {**default_providers(settings).given(), **(config.providers.given() if config.providers else {}), **(providers or {})}
        factories = await FactorySet.bind(chosen, ctx)
        tracer = Tracer(
            [*factories.telemetry.sinks(), *(extra_sinks or [])],
            export=unit.manifest.export.id,
            export_version=unit.manifest.export.version,
            modified=bool(unit.modified_entities),
        )
        engine = cls(unit, settings, factories, tracer)
        engine.provider_mode = (config.providers.mode if config.providers else "") or "default"
        engine.required_kinds = {kind for kind, provider in factories.providers.items() if any(m.name == "arc" for m in provider.members())}  # /readyz waits for ARC
        if settings.profile == "production":
            engine.required_kinds |= {"authn", "authz"}  # production does not serve with an identity layer that reports itself unfit
        await engine.start()
        return engine

    async def start(self) -> None:
        """Prepare local resources, load client plugins, create the runtime and register the agent."""
        for u in self.unit.all_units():
            for node in u.graph.nodes:
                await self._prepare_node(u, node)
                for body_node in iteration_body(node):
                    await self._prepare_node(u, body_node)
        self.adk_plugins = await self.factories.adk_plugin.plugins(self)  # client plugins; KayaTracePlugin is added by the adapters
        self.runtime = select_adk_runtime(self)
        await self._register()
        self.tracer.emit(
            "engine.started",
            core=CORE_VERSION,
            mode=self.provider_mode,
            providers=self.factories.describe(),
            health=self.factories.health,
            modified_entities=self.unit.modified_entities,
        )

    def registration_document(self) -> dict[str, Any]:
        """What a platform registry needs to know about this agent. No secrets, no prompts."""
        m = self.unit.manifest
        return {
            "export": m.export.model_dump(),
            "source": {"kind": m.source.kind, "workflow_id": m.source.workflow_id, "plan_hash": m.source.plan_hash},
            "core": CORE_VERSION,
            "adapter": m.target.adapter,
            "entities": m.entities,
            "children": m.children,
            "runtime_requirements": m.runtime_requirements,
            "exclusions": m.exclusions,
            "providers": [{"kind": p["kind"], "provider": p["provider"]} for p in self.factories.describe()],
        }

    async def _register(self) -> None:
        try:
            await self.factories.registration.register(self.registration_document())
        except Exception as exc:  # a registry outage must not stop an agent that has everything it needs; the trace records it
            self.tracer.emit("registration.failed", error=f"{type(exc).__name__}: {exc}")

    def spawn(self, coro: Any, name: str = "background") -> Any:
        """Run background work (an asynchronous child run, for example) and keep it from being garbage collected."""
        import asyncio

        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def done(t: Any) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.tracer.emit("background.error", task=name, error=f"{type(t.exception()).__name__}: {t.exception()}")

        task.add_done_callback(done)
        return task

    async def drain(self) -> None:
        """Wait for background work started so far (and any it starts)."""
        import asyncio

        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        """Wait for background work, then close the factories."""
        await self.drain()
        await self.factories.aclose()

    # ------------------------------------------------------------------ per-unit resources

    async def model_for(self, unit: ContentUnit, ref: str) -> BaseLlm:
        """The model for a catalog reference, created once per unit and reference."""
        key = (unit.scope, ref)
        if key not in self._models:
            found = resolve_model(unit, ref)
            self._models[key] = found if isinstance(found, BaseLlm) else await self._with_fallbacks(unit, found)
        return self._models[key]

    async def _with_fallbacks(self, unit: ContentUnit, spec: ModelSpec) -> BaseLlm:
        """The model for ``spec``; with fallbacks, an ADK ``FallbackModel`` that moves on to the next one on HTTP 429 or 5xx.
        A fallback without a key is left out, so an optional provider is used only when its key is set."""
        factory = self.factories.model
        primary = await factory.create(spec)
        backups: list[BaseLlm] = []
        for ref in spec.fallbacks:
            found = resolve_model(unit, ref)
            if isinstance(found, BaseLlm):
                backups.append(found)
            elif await factory.ready(found):
                backups.append(await factory.create(found))
            else:
                log.info("fallback model '%s' of '%s' has no key set; left out", ref, spec.id)
        if not backups:
            return primary
        from google.adk.models import FallbackModel

        with warnings.catch_warnings():
            # ADK announces FallbackModel as experimental once per process, outside its ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS switch
            warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\] feature .*FALLBACK_MODEL", category=UserWarning)
            return FallbackModel(models=[primary, *backups])

    async def prompt(self, unit: ContentUnit, prompt_id: str) -> PromptSpec:
        """Fetch a prompt by id for a unit."""
        return await self.factories.prompt.get(prompt_id, None, unit.scope)

    def tool(self, unit: ContentUnit, name: str) -> ToolHandle:
        """The tool handle prepared for a unit.

        Raises:
            ConfigError: The tool is not available in this export.
        """
        try:
            return self._tools[(unit.scope, name)]
        except KeyError as exc:
            raise ConfigError(f"tool '{name}' is not available in this export (check apis/ and mcp/ and the MCP server)") from exc

    def tool_defs_for(self, unit: ContentUnit, names: list[str]) -> list[ToolHandle]:
        """Tool handles for several tool names."""
        return [self.tool(unit, n) for n in names]

    def route_fn(self, unit: ContentUnit, node: Node) -> Callable[..., Any] | None:
        """The compiled route function of a node, or None when it has no condition."""
        if not node.condition_id:
            return None
        key = (unit.scope, node.id)
        if key not in self._route_fns:
            self._route_fns[key] = build_route_function(unit.condition_source(node.condition_id))
        return self._route_fns[key]

    async def _prepare_node(self, unit: ContentUnit, node: Node) -> None:
        """Resolve async resources a node needs before the ADK workflow is built."""
        names = list(node.config.get("tools") or [])
        te = node.config.get("tool_executor_config") or {}
        names += [n for n in te.get("tools", []) if n not in names]
        for name in names:
            if (unit.scope, name) in self._tools:
                continue
            if name in unit.tool_handles:
                self._tools[(unit.scope, name)] = unit.tool_handles[name]
            elif name in unit.tool_defs:
                self._tools[(unit.scope, name)] = self.factories.api.build(unit.tool_defs[name])
            elif name in unit.rag_defs:
                from ..factories.retrieval_tool import RetrievalToolHandle

                self._tools[(unit.scope, name)] = RetrievalToolHandle(unit.rag_defs[name], self.factories.retrieval)
        for server, mdef in unit.mcp_defs.items():
            if any((unit.scope, n) in self._tools for n in [server]):
                continue
            if self._mcp_needed(unit, mdef, names):
                for handle in await self.factories.mcp.build(mdef):
                    self._tools[(unit.scope, handle.name)] = handle
        for ref in (node.config.get("model"), (node.config.get("validation") or {}).get("model")):
            if ref:
                await self.model_for(unit, ref)

    @staticmethod
    def _mcp_needed(unit: ContentUnit, mdef: Any, names: list[str]) -> bool:
        selected = set(mdef.config.get("selected_tools") or [])
        return bool(selected & set(names)) or not selected

    # ------------------------------------------------------------------ plans

    def plan_for(self, unit: ContentUnit, node: Node) -> NodePlan:
        """The execution plan of a node, built once per unit and node."""
        key = (unit.scope, node.id)
        if key not in self._plans:
            from .executors import build_plan

            self._plans[key] = build_plan(self, unit, node)
        return self._plans[key]


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)
