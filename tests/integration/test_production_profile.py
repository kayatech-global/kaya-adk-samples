"""The production profile refuses a run host that would let anonymous callers in."""

import httpx
import pytest

from retail_kyc_onboarding._engine.app import FlowSettings
from retail_kyc_onboarding._engine.server import create_app
from retail_kyc_onboarding._engine.testing.offline import offline_engine


@pytest.fixture
async def production_engine(workflow):
    engine = await offline_engine(workflow, FlowSettings(profile="production"), approve=True)
    try:
        yield engine
    finally:
        await engine.aclose()


async def test_anonymous_authn_is_not_ready_and_refuses_callers_in_production(production_engine):
    app = create_app(engine=production_engine)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = (await client.get("/healthz")).json()
        ready = await client.get("/readyz")
        run = await client.post("/v1/runs", json={"message": "hello"})
    assert health["status"] == "degraded" and "authn" in health["unhealthy"]
    assert ready.status_code == 503 and "authn" in ready.text
    assert run.status_code == 401, run.text


async def test_anonymous_authn_still_serves_in_development(offline_engine):
    app = create_app(engine=offline_engine)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.post("/v1/runs", json={"message": "hello"})).status_code == 200
