"""The Retail KYC Onboarding workflow exported from Kaya, as typed constants: `graph.py` is the core definition (steps and
edges), `components/` holds what it refers to (prompts, models, tools, branch conditions, guardrails).

Generated: do not edit. Replace a prompt, model or tool in settings.py, or change the flow in Kaya and export again."""

# ruff: noqa: E501, S105, S106
from typing import Final

from .._engine.content import ExportContent, ExportIdentity, ExportRef, SecretUse, SourceInfo, TargetInfo
from . import graph
from .components import conditions, guardrails, models, prompts, tools

VERSION: Final = "export-0001"

IDENTITY: Final = ExportIdentity(
    export=ExportRef(id="plan-retail_kyc_onboarding", version="export-0001"),
    source=SourceInfo.model_validate({
        "approved_status": "APPROVED_FOR_EXECUTION",
        "kind": "plan",
        "plan_hash": "0b5b32d89e524b54632c069dc8fa4c717413eb6b5fafb63581c7a3879c0ad3d7",
        "plan_id": "3e2e6fee-abfe-48af-ba66-6ac35f719589",
        "plan_version": 1,
        "workflow_id": "91f24cc0-1ae0-4a43-b43b-c57a92d6daad",
        "workflow_name": "Retail KYC Onboarding",
        "workflow_version": 1,
        "workspace_id": "5ea2d2cb-1b8b-43aa-9cd3-08de6fa1c4c5",
    }),
    target=TargetInfo(adk="2.12.0", adapter="adk_v2", template_set="adk_v2/6.0.0", bundle_schema=1),
    core_requires=">=0.1,<1",
    runtime_requirements={"secrets": ["model_gpt_5_5_api_key"]},
    exclusions=[],
)

SECRETS: Final[tuple[SecretUse, ...]] = (
    SecretUse(name="model_gpt_5_5_api_key", kind="api_key", used_by=("model:gpt-5.5",)),
)

WORKFLOW: Final = ExportContent(
    identity=IDENTITY,
    graph=graph.GRAPH,
    version=VERSION,
    prompts=prompts.PROMPTS,
    models=models.MODELS,
    tools=tools.TOOLS,
    conditions=conditions.CONDITIONS,
    guardrails=guardrails.GUARDRAILS,
    secrets=SECRETS,
    children={},
    graph_sha256=graph.GRAPH_SHA256,
)
