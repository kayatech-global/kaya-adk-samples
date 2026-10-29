"""ADK 2.x adapter. Imported lazily, so importing the engine does not load the ADK workflow runtime."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "AdkV2Runtime":
        from .adapter import AdkV2Runtime

        return AdkV2Runtime
    raise AttributeError(name)


__all__ = ["AdkV2Runtime"]
