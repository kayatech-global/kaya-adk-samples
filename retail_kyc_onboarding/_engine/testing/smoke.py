"""Offline smoke test of an export: run one message on mock providers and check the run follows the graph."""

from __future__ import annotations

from ..content import ExportContent
from ..datatypes import Principal
from ..engine.host import RunHost
from .offline import offline_engine
from .trajectory import check_trajectory


async def run_offline_smoke(content: ExportContent, message: str = "hello") -> list[str]:
    """Run one message through ``content`` with mock providers and return the problems found; empty means it followed the graph."""
    engine = await offline_engine(content, approve=True)
    try:
        res = await RunHost(engine).start(message=message, principal=Principal("smoke-test", roles=("offline-approver",)))
        if res.status == "error":
            return [f"the run failed: {res.error}"]
        return check_trajectory(engine.factories.telemetry.sink.records, engine.unit.graph)
    finally:
        await engine.aclose()
