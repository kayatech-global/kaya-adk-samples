"""Loading, building and serving the agent raises no warning, so `adk run`, `adk web` and the FastAPI server start clean."""

import warnings

from retail_kyc_onboarding import agent, server
from retail_kyc_onboarding._engine.testing.offline import offline_providers
from retail_kyc_onboarding.workflow import WORKFLOW


async def test_building_the_app_and_the_server_raises_no_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await agent.abuild(providers=offline_providers(WORKFLOW))
        server.create_app(session_service_uri="memory://", use_local_storage=False)
    assert [f"{w.category.__name__}: {w.message}" for w in caught] == []
