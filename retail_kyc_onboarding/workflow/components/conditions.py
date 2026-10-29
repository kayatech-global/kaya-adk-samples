"""The branch conditions of plan-retail_kyc_onboarding: checked module source, compiled in a sandbox at start-up (docs/reference/workflow.md)."""

# ruff: noqa: E501, S105, S106
from typing import Final, Literal

from ..._engine.content import Condition

ASSESS_KYC_RISK: Final = Condition(
    id="assess_kyc_risk",
    source=(
        "\"\"\"Generated branch condition for plan node 'assess_kyc_risk'. Do not edit; change the plan and re-export.\"\"\"\n"
        "\n"
        "LABELS = ['edd_review', 'reject', 'standard_review']\n"
        "\n"
        "\n"
        "def route(result, is_error, workflow_variables):\n"
        "    \"\"\"Return the label of the edge to take, or None when no branch matches.\"\"\"\n"
        "    next = None\n"
        "    rec = str(workflow_variables.get('kyc_risk_agent.recommendation') or '').strip().upper()\n"
        "    risk = str(workflow_variables.get('kyc_risk_agent.kyc_risk') or '').strip().upper()\n"
        "    if 'REJECT' in rec or risk == 'HIGH':\n"
        "        next = 'reject'\n"
        "    elif 'EDD' in rec or risk == 'MEDIUM':\n"
        "        next = 'edd_review'\n"
        "    elif 'APPROVE' in rec or risk == 'LOW':\n"
        "        next = 'standard_review'\n"
        "    else:\n"
        "        next = 'reject'\n"
        "    return next\n"
    ),
    labels=("edd_review", "reject", "standard_review"),
    sha256="sha256:178eed5618736c3f5ef720fe8f86de38195c0d374b611761107076d689c03291",
)

CREATE_ACTIVE_ACCOUNT: Final = Condition(
    id="create_active_account",
    source=(
        "\"\"\"Generated branch condition for plan node 'create_active_account'. Do not edit; change the plan and re-export.\"\"\"\n"
        "\n"
        "LABELS = ['created', 'creation_failed']\n"
        "\n"
        "\n"
        "def route(result, is_error, workflow_variables):\n"
        "    \"\"\"Return the label of the edge to take, or None when no branch matches.\"\"\"\n"
        "    next = None\n"
        "    if is_error:\n"
        "        next = 'creation_failed'\n"
        "    else:\n"
        "        status = str(workflow_variables.get('account_creation.account_status') or '').strip().lower()\n"
        "        account_number = str(workflow_variables.get('account_creation.account_number') or '').strip()\n"
        "        if status == 'active' and account_number:\n"
        "            next = 'created'\n"
        "        else:\n"
        "            next = 'creation_failed'\n"
        "    return next\n"
    ),
    labels=("created", "creation_failed"),
    sha256="sha256:ab691b95fd337f4d3555104df0576ff267ec1beafb3024a02b36b67118f7948a",
)

LOOK_UP_ID_REGISTRY: Final = Condition(
    id="look_up_id_registry",
    source=(
        "\"\"\"Generated branch condition for plan node 'look_up_id_registry'. Do not edit; change the plan and re-export.\"\"\"\n"
        "\n"
        "LABELS = ['hard_reject', 'registry_clear']\n"
        "\n"
        "\n"
        "def route(result, is_error, workflow_variables):\n"
        "    \"\"\"Return the label of the edge to take, or None when no branch matches.\"\"\"\n"
        "    next = None\n"
        "    def norm(v):\n"
        "        return re.sub(r'[^a-z0-9]+', '', str(v or '').lower())\n"
        "\n"
        "    if is_error:\n"
        "        next = 'hard_reject'\n"
        "    else:\n"
        "        registry_id = str(workflow_variables.get('id_registry_lookup.registry_id_number') or '').strip()\n"
        "        registered_name = str(workflow_variables.get('id_registry_lookup.registered_name') or '').strip()\n"
        "        supplied_name = str(workflow_variables.get('kyc_intake_agent.full_name') or '').strip()\n"
        "        supplied_id = str(workflow_variables.get('kyc_intake_agent.id_number') or '').strip()\n"
        "        status = str(workflow_variables.get('id_registry_lookup.id_status') or '').strip().lower()\n"
        "        sanctions = str(workflow_variables.get('id_registry_lookup.sanctions_hit') or '').strip().lower()\n"
        "        no_record = not registry_id and not registered_name\n"
        "        sanctions_hit = sanctions in {'true', 'yes', 'y', '1', 'hit', 'match', 'matched'}\n"
        "        inactive = status != 'active'\n"
        "        name_mismatch = bool(supplied_name and registered_name and norm(supplied_name) != norm(registered_name))\n"
        "        no_id = norm(supplied_id) in {'', 'null', 'none'}\n"
        "        id_mismatch = norm(supplied_id) != norm(registry_id)\n"
        "        if no_record or no_id or id_mismatch or sanctions_hit or inactive or name_mismatch:\n"
        "            next = 'hard_reject'\n"
        "        else:\n"
        "            next = 'registry_clear'\n"
        "    return next\n"
    ),
    labels=("hard_reject", "registry_clear"),
    sha256="sha256:9d41dd3a8e8540456c90d4216c00363f3e720cfd0b2a1c3f9153b02bf42708d3",
)

COMPLIANCE_APPROVAL_REVIEW: Final = Condition(
    id="compliance_approval_review",
    source=(
        "\"\"\"Generated branch condition for plan node 'compliance_approval_review'. Do not edit; change the plan and re-export.\"\"\"\n"
        "\n"
        "LABELS = ['approve', 'followup', 'stop']\n"
        "\n"
        "\n"
        "def route(result, is_error, workflow_variables):\n"
        "    \"\"\"Return the label of the edge to take, or None when no branch matches.\"\"\"\n"
        "    next = None\n"
        "    value = (result or '').strip().lower()\n"
        "    stop_terms = ['stop', 'reject', 'decline', 'deny', 'do not proceed', 'do not approve', 'not approved', 'hold']\n"
        "    approve_terms = ['approve', 'approved', 'proceed', 'continue', 'go ahead']\n"
        "    if any(term in value for term in stop_terms):\n"
        "        next = 'stop'\n"
        "    elif any(term in value for term in approve_terms):\n"
        "        next = 'approve'\n"
        "    else:\n"
        "        next = 'followup'\n"
        "    return next\n"
    ),
    labels=("approve", "followup", "stop"),
    sha256="sha256:802dbe8fad488ea83103d9d272193f611a6d0c50eeb40205acd384465e22053d",
)

ENHANCED_DUE_DILIGENCE_REVIEW: Final = Condition(
    id="enhanced_due_diligence_review",
    source=(
        "\"\"\"Generated branch condition for plan node 'enhanced_due_diligence_review'. Do not edit; change the plan and re-export.\"\"\"\n"
        "\n"
        "LABELS = ['approve', 'followup', 'stop']\n"
        "\n"
        "\n"
        "def route(result, is_error, workflow_variables):\n"
        "    \"\"\"Return the label of the edge to take, or None when no branch matches.\"\"\"\n"
        "    next = None\n"
        "    value = (result or '').strip().lower()\n"
        "    stop_terms = ['stop', 'reject', 'decline', 'deny', 'do not proceed', 'do not approve', 'not approved', 'hold']\n"
        "    approve_terms = ['approve', 'approved', 'proceed', 'continue', 'go ahead']\n"
        "    if any(term in value for term in stop_terms):\n"
        "        next = 'stop'\n"
        "    elif any(term in value for term in approve_terms):\n"
        "        next = 'approve'\n"
        "    else:\n"
        "        next = 'followup'\n"
        "    return next\n"
    ),
    labels=("approve", "followup", "stop"),
    sha256="sha256:0509a67b6c50210b544c7534a9619f06bf4af2d51ec1299085dc2205ed2ee8d5",
)

ConditionId = Literal["assess_kyc_risk", "compliance_approval_review", "create_active_account", "enhanced_due_diligence_review", "look_up_id_registry"]
CONDITIONS: Final[dict[str, Condition]] = {
    "assess_kyc_risk": ASSESS_KYC_RISK,
    "compliance_approval_review": COMPLIANCE_APPROVAL_REVIEW,
    "create_active_account": CREATE_ACTIVE_ACCOUNT,
    "enhanced_due_diligence_review": ENHANCED_DUE_DILIGENCE_REVIEW,
    "look_up_id_registry": LOOK_UP_ID_REGISTRY,
}
