"""What you can change in Retail KYC Onboarding without touching the exported workflow in `workflow/`.

* `Settings`: `FLOW_` variables from the environment or the `.env` beside this file (the same file `adk run` and `adk web` load).
  None is required; ADK reads its own variables (`GOOGLE_API_KEY`, ...) itself.
* `build_providers(settings)`: the components the workflow runs with (identity, secrets, models, tools, sessions, telemetry),
  chosen per mode by `runtime.yaml`; code in this function wins over the file.
* `MODELS`, `PROMPTS`, `TOOLS`: replace one exported model, prompt or tool by id. The ids are typed, so a misspelt id is a
  mypy error, and at start-up an error that lists the known ids. The trace reports a replacement as `<kind>:<id> (override)`.

DEMO EXPORT: the default providers authenticate nobody and allow everything; see docs/SECURITY.md before real use.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv
from pydantic_settings import SettingsConfigDict

from ._engine.app import AppConfig, FlowSettings, Overrides, Providers
from ._engine.profile import profile_providers
from .workflow import WORKFLOW

if TYPE_CHECKING:
    from google.adk.models.base_llm import BaseLlm

    from ._engine.content import ModelSpec, Prompt, ToolDef
    from ._engine.factories.api import ToolHandle
    from .workflow.components.models import ModelId
    from .workflow.components.prompts import PromptId
    from .workflow.components.tools import ToolId

ENV_FILE = Path(__file__).with_name(".env")


class Settings(FlowSettings):
    """``FlowSettings`` from `FLOW_` variables and the `.env` beside this file (docs/reference/settings.md lists them)."""

    model_config = SettingsConfigDict(env_prefix="FLOW_", env_file=ENV_FILE, extra="ignore")


def build_providers(settings: Settings) -> Providers:
    """The providers to run with: the built-in defaults with the profile of ``runtime.yaml`` applied (``FLOW_PROVIDER_MODE`` or
    the file's ``mode``; docs/how-to/configure-providers.md). Change fields before returning to override the profile, for
    example ``p.authn = JwtOidcAuthn({...})`` (docs/EXTENDING.md).
    """
    p = profile_providers(settings, content=WORKFLOW)
    return p


MODELS: dict[ModelId, ModelSpec | BaseLlm] = {}
PROMPTS: dict[PromptId, str | Prompt] = {}
TOOLS: dict[ToolId, ToolDef | ToolHandle] = {}


def build_config(
    *,
    settings: Settings | None = None,
    providers: Providers | None = None,
    models: Mapping[str, Any] | None = None,
    prompts: Mapping[str, Any] | None = None,
    tools: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Settings, providers and overrides for one build. Arguments win over the maps above; providers are new for every call."""
    load_dotenv(ENV_FILE, override=False)  # the same keys as `adk run`, also for uvicorn and tests; set variables win
    s = settings or Settings()
    mine = Overrides(
        models={str(k): v for k, v in MODELS.items()},
        prompts={str(k): v for k, v in PROMPTS.items()},
        tools={str(k): v for k, v in TOOLS.items()},
    )
    given = Overrides(models=dict(models or {}), prompts=dict(prompts or {}), tools=dict(tools or {}))
    chosen = providers if providers is not None else build_providers(s)
    from_yaml = chosen.overrides if chosen is not None else None
    return AppConfig(settings=s, providers=chosen, overrides=Overrides().merged(from_yaml).merged(mine).merged(given))
