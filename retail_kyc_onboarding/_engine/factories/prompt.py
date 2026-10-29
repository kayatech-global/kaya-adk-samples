"""prompt: prompt text and metadata for an id and version."""

from __future__ import annotations

import hashlib
from abc import abstractmethod
from dataclasses import replace

from ..datatypes import PromptSpec
from ..errors import KayaAdkError
from .base import Provider, ProviderSettings
from .registry import register


class PromptNotFound(KayaAdkError):
    """Raised when the provider has no such prompt id or version."""

    def __init__(self, prompt_id: str, version: str | None, provider: str):
        super().__init__(f"prompt '{prompt_id}' version '{version or 'active'}' not found by provider '{provider}'")


class PromptFactory(Provider):
    """Factory kind that supplies prompt text and metadata for an id and version."""

    kind = "prompt"

    @abstractmethod
    async def get(self, prompt_id: str, version: str | None = None, scope: str = "") -> PromptSpec:
        """scope: content root relative to the export (\"\" for the export itself, children/<id>/<v> for a Sub-IFLOW)."""


@register
class ContentPrompts(PromptFactory):
    """The prompts of the export itself, with the client's overrides applied. The default provider."""

    name = "content"

    async def get(self, prompt_id: str, version: str | None = None, scope: str = "") -> PromptSpec:
        """The prompt ``prompt_id`` of the unit ``scope``.

        Raises:
            PromptNotFound: The unit has no such prompt, or not at ``version``.
        """
        unit = self.ctx.content_units.get(scope)
        prompt = unit.prompts.get(prompt_id) if unit is not None else None
        if prompt is None or (version is not None and version != prompt.version):
            raise PromptNotFound(prompt_id, version, self.name)
        return prompt if prompt.sha256 else replace(prompt, sha256=_sha(prompt.text))


@register
class MockPrompts(PromptFactory):
    """Test provider: prompts come from settings and the version defaults to ``v1``."""

    name = "mock"

    class Settings(ProviderSettings):
        prompts: dict[str, str] = {}

    async def get(self, prompt_id: str, version: str | None = None, scope: str = "") -> PromptSpec:
        """Return the configured text for ``prompt_id``.

        Raises:
            PromptNotFound: The id is not in settings.
        """
        if prompt_id not in self.settings.prompts:
            raise PromptNotFound(prompt_id, version, self.name)
        text = self.settings.prompts[prompt_id]
        return PromptSpec(prompt_id, version or "v1", text, {}, _sha(text))


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
