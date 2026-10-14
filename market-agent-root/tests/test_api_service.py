import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest
from pydantic import ValidationError

from market_agent.analysis import ai_analysis, metrics
from market_agent.models import Candle
from market_agent.network import HTTP, SourceError
from market_agent.providers import validate_candles


def candles(n=25):
    return [
        Candle(
            day=date(2026, 9, 1) + timedelta(days=i),
            open=100 + i,
            high=102 + i,
            low=99 + i,
            close=101 + i,
            volume=100,
        )
        for i in range(n)
    ]


def test_metrics_exact_baseline_and_sma():
    result = metrics(candles())
    assert result["weekly_change_pct"] == round((125 / 118 - 1) * 100, 4)
    assert result["sma20"] == 115.5
    assert result["weekly_baseline_date"] == "2026-09-18"
    assert result["volume_ratio"] == 1


def test_short_history_no_invented_indicators():
    result = metrics(candles(2))
    assert result["sma20"] is None and result["weekly_change_pct"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0])
def test_bad_prices(value):
    with pytest.raises(ValidationError):
        Candle(day=date.today(), open=1, high=2, low=1, close=value, volume=0)


def test_ohlc_order():
    with pytest.raises(ValidationError):
        Candle(day=date.today(), open=3, high=2, low=1, close=1, volume=0)


def test_duplicate_and_future_candles_rejected():
    data = candles(2)
    with pytest.raises(SourceError):
        validate_candles(data + data, date(2026, 9, 1), date(2026, 9, 30))
    with pytest.raises(SourceError):
        validate_candles(data, date(2026, 8, 1), date(2026, 8, 31))


@pytest.mark.parametrize(
    "payload,valid",
    [
        ({"summary": "Обзор", "risks": [], "source_ids": ["known"]}, True),
        ({"summary": "Обзор", "risks": [], "source_ids": ["invented"]}, False),
        ({"summary": "", "risks": [], "source_ids": []}, False),
        ({"summary": "Обзор", "risks": [], "source_ids": [], "extra": "x"}, False),
    ],
)
def test_ai_schema_and_sources(settings, payload, valid):
    async def run():
        def handle(request):
            body = json.loads(request.content)
            assert "НЕПРОВЕРЕННЫЙ" in body["messages"][0]["content"]
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            call = ai_analysis(HTTP(client, 0), settings, {"news": [{"id": "known"}]})
            if valid:
                assert (await call)["source_ids"] == ["known"]
            else:
                with pytest.raises(SourceError):
                    await call

    asyncio.run(run())
