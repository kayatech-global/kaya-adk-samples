"""Model gateways. ``gateway_openai`` talks to any OpenAI-compatible endpoint (a client's central LLM gateway, Azure
API Management, LiteLLM proxy, vLLM). ``GatewayAdapter`` is the base to subclass for a gateway with its own headers,
token flow or wire protocol: override ``endpoint``, ``wire_model``, ``request_headers`` or ``generate``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import PrivateAttr

from ..datatypes import ModelSpec, Principal
from ..errors import ConfigError
from .base import ProviderSettings
from .model import CURRENT_PRINCIPAL, ModelFactory
from .registry import register


class GatewayLlm(LiteLlm):
    """An ADK model whose endpoint, name and headers are decided per call by a GatewayAdapter."""

    _adapter: Any = PrivateAttr(default=None)
    _spec: Any = PrivateAttr(default=None)

    def __init__(self, adapter: GatewayAdapter, spec: ModelSpec):
        super().__init__(model=adapter.wire_model(spec))
        self._adapter, self._spec = adapter, spec

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        """Stream responses by delegating to the adapter with the principal of the current run."""
        async for response in self._adapter.generate(self._spec, llm_request, stream, CURRENT_PRINCIPAL.get()):
            yield response


class GatewayAdapter(ModelFactory):
    """Base for gateway providers. The defaults implement an OpenAI-compatible gateway."""

    name = ""

    class Settings(ProviderSettings):
        base_url: str = ""
        auth_secret: str = ""  # logical secret name holding the gateway key or token
        auth_header: str = "Authorization"  # header carrying it
        auth_scheme: str = "Bearer"  # prefix for the header value ("" for none)
        headers: dict[str, str] = {}  # static extra headers, e.g. cost centre
        model_map: dict[str, str] = {}  # entity id or model name -> name the gateway expects
        model_prefix: str = "openai"  # LiteLLM route; "openai" means OpenAI wire format
        timeout: float = 120
        default_model: str = ""  # the gateway's model name for every step that named none (see ModelSpec.default); sent as written

    def endpoint(self, spec: ModelSpec) -> str:
        """Return the gateway base URL, preferring the model spec's value; raise ConfigError when neither is set."""
        url = spec.base_url or self.settings.base_url
        if not url:
            raise ConfigError(f"model gateway '{self.name}' needs base_url (provider setting or model spec)")
        return url

    def wire_model(self, spec: ModelSpec) -> str:
        """Return the model name sent to the gateway: mapped through ``model_map``, then prefixed with ``model_prefix`` when one is set."""
        if spec.default and self.settings.default_model:
            return self.settings.default_model
        name = self.settings.model_map.get(spec.id) or self.settings.model_map.get(spec.model_name) or spec.model_name or spec.id
        return (
            name
            if "/" in name and self.settings.model_prefix == ""
            else f"{self.settings.model_prefix}/{name}"
            if self.settings.model_prefix
            else name
        )

    async def request_headers(self, spec: ModelSpec, principal: Principal) -> dict[str, str]:
        """Return the headers for one call: static settings, then the model spec, then the credentials provider. Later sources win.

        Override for a gateway with its own header scheme.
        """
        headers = dict(self.settings.headers)
        headers.update(dict(spec.headers))
        extra = await self.ctx.provider("credentials").get(f"model:{spec.id}", principal, None)
        headers.update(dict(extra.headers))
        return headers

    async def api_key(self, spec: ModelSpec) -> str:
        """Return the gateway key from the secret factory, or an empty string when no secret is named."""
        secret = self.settings.auth_secret or spec.api_key_secret
        return await self.ctx.provider("secret").get(secret) if secret else ""

    async def generate(
        self, spec: ModelSpec, llm_request: LlmRequest, stream: bool, principal: Principal
    ) -> AsyncGenerator[LlmResponse, None]:
        """Yield the gateway's responses for ``llm_request`` through one LiteLLM call.

        The default Bearer ``Authorization`` form passes the key as ``api_key``. Any other header or scheme is sent as an explicit header and ``api_key`` is a placeholder, so LiteLLM adds no header of its own.
        Override to implement a gateway with its own wire protocol.
        """
        headers = await self.request_headers(spec, principal)
        key = await self.api_key(spec)
        kwargs: dict[str, Any] = {"api_base": self.endpoint(spec), "timeout": self.settings.timeout, "drop_params": True}  # a setting the model rejects is dropped, as in LiteLlmModels
        if key and self.settings.auth_header.lower() == "authorization" and self.settings.auth_scheme.lower() == "bearer":
            kwargs["api_key"] = key
        else:
            kwargs["api_key"] = "unused"
            if key:
                headers[self.settings.auth_header] = f"{self.settings.auth_scheme} {key}".strip()
        if headers:
            kwargs["extra_headers"] = headers
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        delegate = LiteLlm(model=self.wire_model(spec), **kwargs)
        async for response in delegate.generate_content_async(llm_request, stream):
            yield response

    async def create(self, spec: ModelSpec) -> BaseLlm:
        """Return an ADK model that routes each call through this adapter."""
        return GatewayLlm(self, spec)


@register
class GatewayOpenAIModels(GatewayAdapter):
    """OpenAI-compatible gateway using the adapter defaults."""

    name = "gateway_openai"
