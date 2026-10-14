import asyncio
from datetime import date, datetime, timezone

import httpx
import pytest
from market_agent.config import Instrument
from market_agent.network import HTTP, SourceError
from market_agent.providers import MOEX, parse_feed, relevant_news

NOW = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
SINCE = datetime(2026, 10, 5, tzinfo=timezone.utc)


def rss(items):
    return ("<rss><channel>" + items + "</channel></rss>").encode()


def item(title="Сбербанк: событие", day="Fri, 09 Oct 2026 10:00:00 +0000", url="https://example.com/news"):
    return f"<item><title>{title}</title><link>{url}</link><pubDate>{day}</pubDate><description>Текст</description></item>"


def test_rss_dates_duplicates_and_keyword_boundaries():
    data, skipped = parse_feed(
        rss(
            item()
            + item()
            + item(day="Fri, 01 Jan 2027 00:00:00 +0000")
            + item(day="Fri, 01 Jan 2021 00:00:00 +0000")
            + item(day="bad")
        ),
        SINCE,
        NOW,
    )
    assert len(data) == 1 and skipped == 1
    assert len(relevant_news(data, Instrument(symbol="SBER", keywords=["Сбербанк"]))) == 1
    assert relevant_news(data, Instrument(symbol="BER", keywords=["банк"])) == []


def test_unsafe_news_link():
    data, skipped = parse_feed(rss(item(url="javascript:alert(1)")), SINCE, NOW)
    assert not data and skipped == 1


def test_atom_feed():
    body = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>SBER</title><link href="https://example.com/a"/><published>2026-10-09T12:00:00Z</published><summary>Example</summary></entry></feed>'
    items, _ = parse_feed(body, SINCE, NOW)
    assert len(items) == 1 and items[0].excerpt == "Example"


def test_xml_entities_rejected():
    with pytest.raises(SourceError):
        parse_feed(b'<!DOCTYPE rss [<!ENTITY a "boom">]><rss><channel>&a;</channel></rss>', SINCE, NOW)


def test_moex_pagination_maps_columns():
    async def run():
        starts = []

        def handle(request):
            offset = int(request.url.params["start"])
            starts.append(offset)
            rows = (
                [
                    ["2026-10-08 00:00:00", 100, 105, 99, 102, 500],
                    ["2026-10-09 00:00:00", 102, 106, 100, 104, 600],
                ]
                if offset == 0
                else []
            )
            assert request.url.params["interval"] == "24"
            return httpx.Response(
                200,
                json={
                    "candles": {"columns": ["begin", "open", "high", "low", "close", "volume"], "data": rows}
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await MOEX(HTTP(client, 0)).candles(
                Instrument(symbol="SBER"), date(2026, 10, 1), date(2026, 10, 11)
            )
        assert starts == [0, 2] and result[-1].close == 104

    asyncio.run(run())


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_http_errors_redacted(status):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(status, text="SECRET_KEY"))
        ) as client:
            with pytest.raises(SourceError) as error:
                await HTTP(client, 0).json("GET", "https://example.com")
            assert "SECRET_KEY" not in str(error.value)

    asyncio.run(run())


def test_http_retries(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("market_agent.network.asyncio.sleep", no_sleep)

    async def run():
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(503 if len(calls) == 1 else 200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert (await HTTP(client, 2).json("GET", "https://example.com"))["ok"]
        assert len(calls) == 2

    asyncio.run(run())


def test_protocol_failure_is_sanitized():
    async def run():
        def broken(request):
            raise httpx.RemoteProtocolError("SECRET upstream detail")

        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            with pytest.raises(SourceError) as error:
                await HTTP(client, 0).fetch("GET", "https://example.com")
            assert "SECRET" not in str(error.value)

    asyncio.run(run())


def test_oversized_source_rejected():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 2_000_001))
        ) as client:
            with pytest.raises(SourceError):
                await HTTP(client, 0).fetch("GET", "https://example.com")

    asyncio.run(run())
