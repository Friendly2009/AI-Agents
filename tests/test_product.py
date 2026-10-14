import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values
from market_agent.collector import NewsCollector
from market_agent.diagnostics import diagnose
from market_agent.models import News
from market_agent.network import HTTP, SourceError
from market_agent.service import Service
from market_agent.setup import configure_live
from market_agent.storage import Store

NOW = datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
FEED = "https://example.com/rss"


def news_item():
    return News(
        id="one",
        title="Сбербанк опубликовал результаты",
        url="https://example.com/1",
        published_at=NOW - timedelta(days=1),
        feed_url=FEED,
    )


def test_setup_preserves_token_database_and_other_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("markets.live.json").write_text(
        json.dumps({"instruments": [{"symbol": "SBER"}], "news_feeds": [FEED]})
    )
    path = Path(".env")
    path.write_text(
        "# custom\nAPI_TOKEN="
        + "t" * 40
        + "\nDATABASE_PATH=data/existing.sqlite\nSCHEDULE_HOUR=13\nMODE=demo\nLLM_API_KEY=old\n"
    )
    configure_live(path, "new-key-" + "x" * 35)
    values = dotenv_values(path)
    assert values["API_TOKEN"] == "t" * 40
    assert values["DATABASE_PATH"] == "data/existing.sqlite"
    assert values["SCHEDULE_HOUR"] == "13"
    assert values["MODE"] == "live" and values["LLM_ENABLED"] == "true"
    assert values["MARKETS_FILE"] == "markets.live.json"
    assert values["LLM_MODEL"] == "gemini-2.5-flash"
    assert values["LLM_API_KEY"] == "new-key-" + "x" * 35
    assert "# custom" in path.read_text()
    assert not list(tmp_path.glob(".env-tmp-*"))


def test_setup_validation_does_not_modify_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path(".env")
    path.write_text("MODE=demo\n")
    with pytest.raises(ValueError):
        configure_live(path, "bad")
    assert path.read_text() == "MODE=demo\n"


def test_news_archive_deduplicates_survives_restart_and_filters_feeds(settings):
    store = Store(settings.database_path)
    store.cache_news([news_item()], [FEED], NOW, [])
    store.cache_news([news_item()], [FEED], NOW + timedelta(minutes=30), [])
    restarted = Store(settings.database_path)
    assert len(restarted.cached_news([FEED], NOW - timedelta(days=7), NOW)) == 1
    assert restarted.cached_news(["https://other.example/rss"], NOW - timedelta(days=7), NOW) == []
    status = restarted.news_status([FEED])
    assert status["collection_started_at"] == NOW.isoformat()
    assert status["last_success_at"] == (NOW + timedelta(minutes=30)).isoformat()


def test_archive_prunes_old_items(settings):
    store = Store(settings.database_path)
    old = news_item().model_copy(update={"published_at": NOW - timedelta(days=50)})
    store.cache_news([old], [FEED], NOW, [])
    assert store.cached_news([FEED], NOW - timedelta(days=60), NOW) == []


def test_failed_poll_keeps_news_and_last_success(settings):
    async def run():
        store = Store(settings.database_path)
        store.cache_news([news_item()], [FEED], NOW, [])
        markets = settings.markets()
        markets.news_feeds = [FEED]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
            collector = NewsCollector(settings, markets, store, HTTP(client, 0))
            status = await collector.refresh(NOW + timedelta(hours=1), force=True)
        assert status["warnings"]
        assert status["last_success_at"] == NOW.isoformat()
        assert len(store.cached_news([FEED], NOW - timedelta(days=7), NOW)) == 1

    asyncio.run(run())


def test_short_interval_does_not_fetch_news_again(settings):
    async def run():
        store = Store(settings.database_path)
        store.cache_news([news_item()], [FEED], NOW, [])
        markets = settings.markets()
        markets.news_feeds = [FEED]

        def no_network(request):
            raise AssertionError("Unexpected duplicate fetch")

        async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as client:
            collector = NewsCollector(settings, markets, store, HTTP(client, 0))
            assert (await collector.refresh(NOW + timedelta(seconds=30)))["item_count"] == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (
            400,
            b'{"error":{"message":"User location is not supported for the API use. KEY_SECRET"}}',
            "регионе",
        ),
        (400, b'{"error":{"message":"API key not valid. KEY_SECRET"}}', "Ключ API"),
        (429, b"KEY_SECRET", "квота"),
        (404, b"KEY_SECRET", "модель"),
    ],
)
def test_actionable_ai_errors_without_secret(status, body, expected):
    async def run():
        calls = []

        def handle(r):
            calls.append(r)
            return httpx.Response(status, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(SourceError) as error:
                await HTTP(client, 3).json("POST", "https://example.com/chat/completions")
            assert expected in str(error.value)
            assert "KEY_SECRET" not in str(error.value)
            assert len(calls) == 1

    asyncio.run(run())


def test_diagnostic_demo_never_claims_live_ready(settings):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            service = Service(settings, settings.markets(), Store(settings.database_path), HTTP(client, 0))
            result = await diagnose(service)
            assert not result["ready"] and result["checks"][0]["name"] == "Режим"

    asyncio.run(run())


def test_archive_keeps_dates_not_collection_time(settings):
    store = Store(settings.database_path)
    store.cache_news([news_item()], [FEED], NOW + timedelta(days=4), [])
    assert store.cached_news([FEED], NOW, NOW + timedelta(days=5)) == []


def test_product_api_routes_require_auth(settings):
    async def run():
        from market_agent.api import create_app

        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.get("/api/status")).status_code == 401
                assert (await client.post("/api/check")).status_code == 401
                headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
                result = await client.get("/api/status", headers=headers)
                assert result.status_code == 200 and "api_key" not in result.text
                check = await client.post("/api/check", headers=headers)
                assert check.status_code == 200 and not check.json()["ready"]
                assert (await client.get("/api/status", headers=headers)).json()["diagnostic"] is not None

    asyncio.run(run())


def test_quota_failure_does_not_repeat_for_second_instrument(settings):
    async def run():
        from market_agent.config import Instrument
        from market_agent.providers import Demo

        settings.mode = "live"
        settings.llm_enabled = True
        markets = settings.markets()
        markets.instruments.append(Instrument(symbol="GAZP"))
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(429)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            service = Service(settings, markets, Store(settings.database_path), HTTP(client, 0))
            service.provider = Demo()
            report = await service.build_report(NOW)
            assert len(calls) == 1
            assert not report["ai_complete"]
            assert all(item["analysis_kind"] == "calculated" for item in report["instruments"])
            assert any("квота" in w for w in report["warnings"])

    asyncio.run(run())
