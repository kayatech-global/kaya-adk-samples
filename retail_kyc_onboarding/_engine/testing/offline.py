"""Configurations with no external access: every provider is a mock. Used by smoke tests and offline demos."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..app import AppConfig, FlowSettings, Providers
from ..content import ExportContent

if TYPE_CHECKING:
    from ..engine.engine import Engine


def offline_providers(
    content: ExportContent | None = None,
    *,
    model_default: str = "ok",
    model_script: list[dict[str, Any]] | None = None,
    api_responses: dict[str, Any] | None = None,
    approve: bool | None = None,
) -> Providers:
    """Providers with no external access: mock models, tools and secrets, memory sessions and trace.

    ``approve`` True/False makes approvals answer themselves (no pause). Every secret the content names has a value.
    """
    from ..factories.api import MockApiFactory
    from ..factories.approval import AutoChannel
    from ..factories.mcp import MockMcpFactory
    from ..factories.model import MockModels
    from ..factories.secret import MockSecrets
    from ..factories.session import MemorySessions
    from ..factories.telemetry import MemoryTelemetry

    secrets: dict[str, str] = {}
    for u in content.all_units() if content else []:
        secrets.update({name: "offline-secret" for name in u.identity.runtime_requirements.get("secrets", [])})
    return Providers(
        secret=MockSecrets({"values": secrets}),
        model=MockModels({"default": model_default, "script": model_script or []}),
        api=MockApiFactory({"responses": api_responses or {}}),
        mcp=MockMcpFactory(),
        session=MemorySessions(),
        telemetry=MemoryTelemetry(),
        approval_channel=AutoChannel({"approve": approve, "roles": ["offline-approver"]}) if approve is not None else None,
    )


async def offline_engine(content: ExportContent, settings: FlowSettings | None = None, **kw: Any) -> Engine:
    """A started engine on ``content`` with ``offline_providers(content, **kw)``."""
    from ..engine.engine import Engine

    config = AppConfig(settings=settings or FlowSettings.model_construct(), providers=offline_providers(content, **kw))
    return await Engine.from_content(content, config)


def providers_by_name(kinds: Mapping[str, Mapping[str, Any]]) -> Providers:
    """Providers chosen by built-in name, for tests and demos: ``{"model": {"provider": "mock", "default": "ok"}}``.

    Each block names the provider and holds its settings. Kinds not given take the defaults.
    """
    from ..factories import builtin  # noqa: F401  (registers built-in providers)
    from ..factories.registry import REGISTRY

    chosen: dict[str, Any] = {}
    for kind, block in kinds.items():
        settings = {k: v for k, v in block.items() if k != "provider"}
        chosen[kind] = REGISTRY.resolve(kind, str(block["provider"]))(settings)
    return Providers(**chosen)
