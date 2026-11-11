from httpx import ASGITransport, AsyncClient

from mock_provider.app import app as mock
from payments.presentation.app import app


async def test_mock_health():
    async with AsyncClient(transport=ASGITransport(app=mock), base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200


async def test_live_and_missing_dependencies():
    # No lifespan: deliberate missing resources tests safe error boundary.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.post("/payments", json={})).status_code == 401
