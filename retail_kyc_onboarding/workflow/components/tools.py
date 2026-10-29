"""The tools of plan-retail_kyc_onboarding, one constant each. Generated: replace a tool in settings.py, not here."""

# ruff: noqa: E501, S105, S106
from typing import Final, Literal

from ..._engine.content import ToolDef

RETAIL_KYC_ACCOUNT_CREATION: Final = ToolDef(
    name="retail_kyc_account_creation",
    type="api",
    description="Creates an active retail account in MockAPI after KYC approval.",
    parameters={
        "customer_id_number": {"description": "Approved customer ID number.", "type": "string"},
        "full_name": {"description": "Approved customer full legal name.", "type": "string"},
        "product": {"description": "Banking product to open for the customer.", "type": "string"},
        "status": {"description": "Account status to create, set to ACTIVE after approval.", "type": "string"},
    },
    config={
        "authorization": {"type": "no-auth"},
        "body_mode": "json",
        "headers": [{"active": True, "name": "Content-Type", "value": "application/json"}],
        "method": "POST",
        "retry_attempts": 3,
        "timeout": 60,
        "url": "https://608b7a57737e470017b74d29.mockapi.io/api/v1/accounts",
    },
    version="v-e72bdb34",
    sha256="sha256:2698b83909fdb9bce40d717b7da817886f99f8f818686f7dba952a2c58c4099c",
)

RETAIL_KYC_ID_REGISTRY_LOOKUP: Final = ToolDef(
    name="retail_kyc_id_registry_lookup",
    type="api",
    description="Looks up a retail KYC customer record by ID number in the MockAPI registry.",
    parameters={"id_number": {"description": "Customer ID number to look up in the registry.", "type": "string"}},
    config={
        "authorization": {"type": "no-auth"},
        "body_mode": "json",
        "headers": [{"active": True, "name": "Content-Type", "value": "application/json"}],
        "method": "GET",
        "retry_attempts": 3,
        "timeout": 60,
        "url": "https://608b7a57737e470017b74d29.mockapi.io/api/v1/customers",
    },
    version="v-f8c55046",
    sha256="sha256:35627d21831fe5e118c0989f0dd08dc66f285cc58f48c9e1008ec5a69243ed72",
)

ToolId = Literal["retail_kyc_account_creation", "retail_kyc_id_registry_lookup"]
TOOLS: Final[dict[str, ToolDef]] = {
    "retail_kyc_account_creation": RETAIL_KYC_ACCOUNT_CREATION,
    "retail_kyc_id_registry_lookup": RETAIL_KYC_ID_REGISTRY_LOOKUP,
}
