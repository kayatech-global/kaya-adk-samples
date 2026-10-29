"""Optional placeholder ``arc`` providers: where an integration with ARC plugs in, if you need one. None is implemented.

How ARC exposes secrets, identity, entitlements, prompts, models and tools (its endpoints, payloads, authentication and errors)
is not known to this export, so no contract is assumed here. Each class below is the empty shape of one factory kind: the engine
side of the contract (what the method receives and must return) is fixed and documented; the ARC side is yours to decide.

To integrate:

1. Fill in the methods of the kinds ARC should serve, replace their ``health()`` with a real check that ARC can be reached,
   and delete the classes it should not serve.
2. Declare the settings your implementation needs on ``ArcSettings`` (for example a base URL); the ``config`` block of the
   ``arc`` profile in ``runtime.yaml`` fills them for every ``arc`` provider.
3. Run with ``FLOW_PROVIDER_MODE=arc``. ``runtime.yaml`` imports this module (``plugins:``) and its ``arc`` profile selects every
   kind that has an ``arc`` provider here (``all: arc``); the other kinds keep their default provider.

The agent runs on the ``default`` mode and never uses these classes unless the ``arc`` mode is selected.
Until a method is filled in it raises ``NotImplementedError`` (``ArcModels.create`` and ``ArcApiFactory.build`` at start-up,
the others when a run first calls them), and until ``health()`` is replaced ``/readyz`` reports the provider as not ready.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._engine.datatypes import (
    Credential,
    Decision,
    ModelSpec,
    Principal,
    PromptSpec,
    ToolDef,
)
from ._engine.factories.api import ApiFactory, ToolHandle
from ._engine.factories.base import HealthStatus, ProviderSettings
from ._engine.factories.mcp import McpFactory
from ._engine.factories.model import BaseLlm, ModelFactory
from ._engine.factories.prompt import PromptFactory
from ._engine.factories.registry import register
from ._engine.factories.secret import SecretFactory
from ._engine.factories.security import Authenticator, Authorizer, CredentialProvider


class ArcSettings(ProviderSettings):
    """The settings every ``arc`` provider takes. Add the fields your ARC needs; unknown keys in ``runtime.yaml`` are rejected."""


class ArcPlaceholder:
    """Shared by the placeholders: the settings class and a health check that says the provider is not implemented yet."""

    name = "arc"
    Settings = ArcSettings

    async def health(self) -> HealthStatus:
        """Replace with a check that ARC can be reached; ``/readyz`` waits for it."""
        return HealthStatus(False, f"arc {self.kind} provider is a placeholder (arc_providers.py)")  # type: ignore[attr-defined]

    def todo(self, method: str) -> NotImplementedError:
        """The error a method raises until it is implemented."""
        return NotImplementedError(f"arc {self.kind} provider: {type(self).__name__}.{method} is a placeholder; implement it in arc_providers.py")  # type: ignore[attr-defined]


@register
class ArcSecrets(ArcPlaceholder, SecretFactory):
    """``secret``: the value of a logical secret name (``api_key_secret`` of a model, a tool's auth secret)."""

    async def get(self, name: str) -> str:
        # Return the secret's value. Raise SecretNotFound(name, self.name) when ARC has no such secret.
        # Never put the value in a log line or an error message.
        raise self.todo("get")


@register
class ArcAuthn(ArcPlaceholder, Authenticator):
    """``authn``: who is calling the run host (REST API, A2A)."""

    async def authenticate(self, request: Mapping[str, Any]) -> Principal:
        # `request` is {"headers": {...}}, lower-case names. Verify the caller with ARC and return
        # Principal(subject, tenant, roles, claims). Raise AuthenticationError when the caller cannot be identified.
        raise self.todo("authenticate")


@register
class ArcAuthz(ArcPlaceholder, Authorizer):
    """``authz``: whether a principal may run the agent, call a tool or decide an approval."""

    async def check(self, principal: Principal, action: str, resource: Mapping[str, Any], context: Mapping[str, Any]) -> Decision:
        # Return Decision("allow" | "deny" | "require_approval", reason). Any other effect is treated as a deny.
        # An exception raised here is a deny (an allow only when the guard is set to fail open).
        raise self.todo("check")


@register
class ArcCredentials(ArcPlaceholder, CredentialProvider):
    """``credentials``: the headers an outbound call (model, tool, remote agent) carries on behalf of the caller."""

    async def get(self, target: str, principal: Principal, scope: str | None = None) -> Credential:
        # `target` names the callee: "tool:<id>", "model:<id>", "mcp:<server>" or "a2a:<node>".
        # Return Credential(headers, expires_at), or an empty Credential() when nothing applies.
        raise self.todo("get")


@register
class ArcPrompts(ArcPlaceholder, PromptFactory):
    """``prompt``: the text of a prompt id from ``workflow/components/prompts.py``."""

    async def get(self, prompt_id: str, version: str | None = None, scope: str = "") -> PromptSpec:
        # Return PromptSpec(id, version, text, meta, sha256). Raise PromptNotFound(prompt_id, version, self.name) when ARC
        # has none; to fall back to the exported prompt instead, ask ContentPrompts(None, self.ctx).get(prompt_id, version, scope).
        raise self.todo("get")


@register
class ArcModels(ArcPlaceholder, ModelFactory):
    """``model``: the ADK model object for a model id from ``workflow/components/models.py``."""

    async def create(self, spec: ModelSpec) -> BaseLlm:
        # Return an ADK BaseLlm for `spec` (spec.model_name, spec.provider, spec.fallbacks, ...). If ARC offers an
        # OpenAI-compatible endpoint, the built-in `gateway_openai` provider may already be enough, with no code here.
        raise self.todo("create")


@register
class ArcApiFactory(ArcPlaceholder, ApiFactory):
    """``api``: how a REST tool from ``workflow/components/tools.py`` is executed."""

    def build(self, tool: ToolDef) -> ToolHandle:
        # Return a ToolHandle subclass whose `invoke(args, call)` executes the tool and returns ToolResult(ok, text, data).
        # `call` carries the run id, the principal and an idempotency key. `build` runs once per tool, when the engine starts.
        raise self.todo("build")


@register
class ArcMcpFactory(ArcPlaceholder, McpFactory):
    """``mcp``: the tools of an MCP server definition."""

    async def build(self, tool: ToolDef) -> list[ToolHandle]:
        # Return one ToolHandle per tool the server offers (tool.config holds url, transport and selected_tools).
        raise self.todo("build")
