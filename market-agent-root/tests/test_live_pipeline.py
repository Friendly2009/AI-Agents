"""Live adapter paths with controlled HTTP responses; no paid API calls."""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from market_agent.config import Instrument
from market_agent.network import HTTP
from market_agent.providers import Demo
from market_agent.service import Service
from market_agent.storage import Store

NOW = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
FEED = b"<rss><channel><item><title>SBER results</title><link>https://example.com/n</link><pubDate>Fri, 09 Oct 2026 10:00:00 +0000</pubDate><description>Results published.</description></item></channel></rss>"


@pytest.mark.parametrize("llm_status", [200, 503])
def test_full_live_pipeline_and_ai_fallback(settings, llm_status):
    async def run():
        settings.mode = "live"
        settings.llm_enabled = True
        settings.llm_model = "test-model"
        markets = settings.markets()
        markets.news_feeds = ["https://example.com/rss"]
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path.endswith("/candles.json"):
                rows = (
                    []
                    if request.url.params["start"] != "0"
                    else [
                        [f"2026-10-{d:02d} 00:00:00", 100, 105, 99, 102 + d / 100, 1000]
                        for d in (1, 2, 5, 6, 7, 8, 9)
                    ]
                )
                return httpx.Response(
                    200,
                    json={
                        "candles": {
                            "columns": ["begin", "open", "high", "low", "close", "volume"],
                            "data": rows,
                        }
                    },
                )
            if request.url.path == "/rss":
                return httpx.Response(200, content=FEED)
            assert request.url.path.endswith("/chat/completions")
            body = json.loads(request.content)
            data = json.loads(body["messages"][1]["content"])
            assert data["metrics"]["last_date"] == "2026-10-09"
            assert data["news"][0]["title"] == "SBER results"
            result = {
                "summary": "Обзор по доступным данным.",
                "risks": ["Ограниченный набор источников"],
                "source_ids": [data["news"][0]["id"]],
            }
            return httpx.Response(
                llm_status,
                json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            store = Store(settings.database_path)
            service = Service(settings, markets, store, HTTP(client, 0))
            report = await service.build_report(NOW)
            run_id = store.claim()
            store.finish(run_id, report=report)
            saved = Store(settings.database_path).get(run_id)
            row = saved["report"]["instruments"][0]
            assert row["analysis_kind"] == ("ai" if llm_status == 200 else "calculated")
            assert saved["status"] == ("completed" if llm_status == 200 else "degraded")
            assert len(row["news"]) == 1
            assert len(requests) == 4

    asyncio.run(run())


def test_partial_instrument_failure(settings):
    async def run():
        from market_agent.network import SourceError

        class Partial(Demo):
            async def candles(self, instrument, start, end):
                if instrument.symbol == "GAZP":
                    raise SourceError("Недоступен инструмент")
                return await super().candles(instrument, start, end)

        markets = settings.markets()
        markets.instruments.append(Instrument(symbol="GAZP"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            service = Service(settings, markets, Store(settings.database_path), HTTP(client, 0))
            service.provider = Partial()
            report = await service.build_report(NOW)
            assert "metrics" in report["instruments"][0]
            assert "error" in report["instruments"][1]
            assert any("GAZP" in warning for warning in report["warnings"])

    asyncio.run(run())


def test_stale_prices_are_marked(settings):
    async def run():
        from datetime import timedelta

        class Old(Demo):
            async def candles(self, instrument, start, end):
                return await super().candles(instrument, start, end - timedelta(days=20))

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            service = Service(settings, settings.markets(), Store(settings.database_path), HTTP(client, 0))
            service.provider = Old()
            report = await service.build_report(NOW)
            assert any("старше" in warning for warning in report["warnings"])

    asyncio.run(run())
