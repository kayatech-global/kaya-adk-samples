"""deploy: a target turns the export into what one platform runs. Targets are providers of kind ``deploy`` in the factory registry.

A target only plans by default: ``plan`` returns the files to write and the commands to run, and nothing touches a platform until the
caller asks ``apply`` to run them. Built-in targets register on import of ``builtin``; a client target is a ``DeployTarget`` subclass
registered with ``register_target`` (or the ``flow_adk.factories`` entry point ``deploy.<name>``).
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from ..errors import ConfigError
from ..factories.base import Provider
from ..factories.registry import REGISTRY

KIND = "deploy"
T = TypeVar("T", bound="DeployTarget")


@dataclass(frozen=True)
class DeployProject:
    """What every target needs to know about the export it deploys. Built from the package, never from the source platform."""

    root: Path  # the agent folder (the export itself): the Docker build context and what `adk deploy` ships
    package: str  # the Python package, also the ADK app name
    export_id: str
    export_version: str
    adk_version: str
    secret_env: tuple[str, ...] = ()  # environment variables of the export's logical secrets (names only)
    python_version: str = "3.13"

    @property
    def agent_dir(self) -> Path:
        """The ADK agent folder, the one `adk deploy` ships."""
        return self.root

    @property
    def service_name(self) -> str:
        """A DNS-safe default name for services and images."""
        return self.package.replace("_", "-")[:63]


@dataclass
class DeployPlan:
    """Files to write (relative to the output folder) and commands to run, in order; ``notes`` are shown to the operator."""

    target: str
    files: dict[str, str] = field(default_factory=dict)
    commands: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def describe(self, out: Path) -> str:
        """A plain-text account of the plan: written files, commands and notes."""
        lines = [f"target: {self.target}"]
        lines += [f"  write {out / rel}" for rel in sorted(self.files)]
        lines += [f"  run   {shlex.join(cmd)}" for cmd in self.commands]
        lines += [f"  note  {note}" for note in self.notes]
        return "\n".join(lines)


class DeployTarget(Provider):
    """Port of the ``deploy`` kind. Subclasses set ``name`` and ``summary``, declare ``Settings`` and implement ``plan``."""

    kind = KIND
    summary: ClassVar[str] = ""

    def plan(self, project: DeployProject, out: Path) -> DeployPlan:
        """Return what deploying ``project`` writes (under ``out``) and runs. Must not touch the network or a platform.

        Raises:
            NotImplementedError: The target is a placeholder.
            ConfigError: A required setting is missing.
        """
        raise NotImplementedError(f"deploy target '{self.name}' has no plan")

    def apply(self, plan: DeployPlan, out: Path, *, run: Callable[..., Any] = subprocess.run) -> None:
        """Write the plan's files under ``out`` and run its commands from the project folder, stopping at the first failure."""
        for rel, text in plan.files.items():
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for cmd in plan.commands:
            run(cmd, check=True)  # an argv list from the plan, never a shell string

    def write(self, plan: DeployPlan, out: Path) -> None:
        """Write only the plan's files (a dry run)."""
        self.apply(DeployPlan(plan.target, plan.files), out)


class PlaceholderTarget(DeployTarget):
    """A platform that is named but not built yet: ``plan`` explains what to implement instead of guessing."""

    guidance: ClassVar[str] = ""

    def plan(self, project: DeployProject, out: Path) -> DeployPlan:
        """Always raises: this target is a placeholder."""
        raise NotImplementedError(
            f"deploy target '{self.name}' is a placeholder. {self.guidance} "
            "Subclass DeployTarget in the agent's deployment/deploy.py and register it with register_target (see the docstring of deployment/deploy.py)."
        )


def register_target(cls: type[T]) -> type[T]:
    """Class decorator for a client's own target: registered over any built-in of the same name."""
    if cls.kind != KIND or not cls.name:
        raise ConfigError(f"{cls.__qualname__} must be a DeployTarget with a 'name' to be registered")
    REGISTRY.register_extra(cls)
    return cls


def target(name: str, settings: dict[str, Any] | None = None) -> DeployTarget:
    """A target by registered name, with its settings validated.

    Raises:
        ProviderNotFound: No target has that name (the message lists the known ones).
    """
    from . import builtin  # noqa: F401  (registers the built-in targets)

    cls = REGISTRY.resolve(KIND, name)
    return cls(settings or {})  # type: ignore[return-value]


def target_names() -> dict[str, str]:
    """Every registered target name, sorted, with its one-line summary."""
    from . import builtin  # noqa: F401

    return {name: getattr(REGISTRY.resolve(KIND, name), "summary", "") for name in REGISTRY.names(KIND)}
