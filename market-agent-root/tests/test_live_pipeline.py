import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from market_agent.api import create_app
from market_agent.config import Settings
from market_agent.network import HTTP, SourceError
from market_agent.service import Service
from market_agent.storage import Store


def test_api_auth_and_no_secret_leak(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        for url in ["/api/runs", "/api/config", "/api/openapi.json"]:
            assert client.get(url).status_code == 401
            assert client.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/api/runs").status_code == 401
        headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        result = client.get("/api/config", headers=headers)
        assert result.status_code == 200 and "api_token" not in result.text
        assert client.get("/api/runs/missing", headers=headers).status_code == 404
        assert client.get("/api/runs?limit=1000", headers=headers).status_code == 422
        assert client.get("/api/openapi.json", headers=headers).status_code == 200


def test_demo_report_no_network(settings):
    async def run():
        def reject(_):
            raise AssertionError("Demo attempted network access")

        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            service = Service(settings, settings.markets(), Store(settings.database_path), HTTP(client, 0))
            report = await service.build_report(datetime(2026, 10, 12, 9, tzinfo=timezone.utc))
            assert report["mode"] == "demo"
            assert report["instruments"][0]["metrics"]["last_date"] == "2026-10-09"
            assert report["instruments"][0]["analysis_kind"] == "calculated"
            assert "ДЕМО" in report["warnings"][0]

    asyncio.run(run())


def test_manual_run_roundtrip(settings):
    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
            ) as client:
                response = await client.post("/api/runs")
                assert response.status_code == 202
                run_id = response.json()["id"]
                await asyncio.gather(*list(app.state.service.tasks))
                result = (await client.get("/api/runs/" + run_id)).json()
                assert result["status"] == "degraded" and result["report"]["mode"] == "demo"
                assert len((await client.get("/api/runs")).json()) == 1

    asyncio.run(run())


def test_upstream_failure_recorded(settings):
    async def run():
        class Broken:
            async def candles(self, *args):
                raise SourceError("Источник недоступен")

        async with httpx.AsyncClient() as client:
            store = Store(settings.database_path)
            service = Service(settings, settings.markets(), store, HTTP(client, 0))
            service.provider = Broken()
            run_id = store.claim()
            await service.execute(run_id)
            assert store.get(run_id)["status"] == "failed"

    asyncio.run(run())


def test_duplicate_manual_run_409(settings):
    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):

            async def slow():
                await asyncio.sleep(100)

            app.state.service.build_report = slow
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
            ) as client:
                assert (await client.post("/api/runs")).status_code == 202
                assert (await client.post("/api/runs")).status_code == 409

    asyncio.run(run())


@pytest.mark.parametrize("token", ["short", "CHANGE_ME_GENERATE_A_RANDOM_TOKEN"])
def test_bad_token_refused(token):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_token=token)


def test_llm_incomplete_config_refused():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_token="x" * 40, llm_enabled=True)
