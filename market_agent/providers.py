import hashlib
import html
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from defusedxml import ElementTree

from .models import Candle, News
from .network import SourceError


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", text))).strip()


def validate_candles(candles, start, end):
    ordered = sorted(candles, key=lambda c: c.day)
    if len({c.day for c in ordered}) != len(ordered):
        raise SourceError("Повторяющиеся даты свечей")
    if any(c.day < start or c.day > end for c in ordered):
        raise SourceError("Свечи за пределами запрошенного периода")
    if len(ordered) < 2:
        raise SourceError("Недостаточно свечей для анализа")
    return ordered


class MOEX:
    def __init__(self, http):
        self.http = http

    async def candles(self, instrument, start: date, end: date):
        url = (
            "https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
            f"{instrument.board}/securities/{instrument.symbol}/candles.json"
        )
        result, offset = [], 0
        for _ in range(20):
            data = await self.http.json(
                "GET",
                url,
                params={
                    "from": start.isoformat(),
                    "till": end.isoformat(),
                    "interval": 24,
                    "start": offset,
                    "iss.meta": "off",
                },
            )
            try:
                block = data["candles"]
                rows = block["data"]
                if not rows:
                    return validate_candles(result, start, end)
                for row in rows:
                    values = dict(zip(block["columns"], row, strict=True))
                    result.append(
                        Candle(
                            day=date.fromisoformat(values["begin"][:10]),
                            **{k: values[k] for k in ("open", "high", "low", "close", "volume")},
                        )
                    )
                offset += len(rows)
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceError("Неверный формат свечей MOEX") from exc
        raise SourceError("Превышен лимит страниц MOEX; неполные данные отклонены")


class Demo:
    async def candles(self, instrument, start, end):
        seed = sum(map(ord, instrument.symbol))
        rows, index = [], 0
        day = start
        while day <= end:
            if day.weekday() < 5:
                value = 100 + seed % 100 + index * 0.3
                rows.append(
                    Candle(
                        day=day,
                        open=value,
                        high=value + 2,
                        low=value - 1,
                        close=value + 0.5,
                        volume=10000 + index * 100,
                    )
                )
                index += 1
            day += timedelta(days=1)
        return rows


def parse_feed(body: bytes, since: datetime, until: datetime):
    """Accept RSS/Atom; reject undated, future, unsafe, duplicate and stale items."""
    try:
        root = ElementTree.fromstring(body)
    except Exception as exc:
        raise SourceError("Некорректный или небезопасный XML новостей") from exc
    ns = "{http://www.w3.org/2005/Atom}"
    nodes = root.findall("./channel/item") + root.findall(f"{ns}entry")
    if root.tag not in {"rss", f"{ns}feed"}:
        raise SourceError("Ожидалась RSS или Atom лента")
    found, seen, skipped = [], set(), 0
    for node in nodes[:1000]:
        atom = node.tag == f"{ns}entry"
        prefix = ns if atom else ""
        title = clean(node.findtext(prefix + "title", ""))[:500]
        link = node.findtext("link", "") if not atom else ""
        if atom:
            for element in node.findall(ns + "link"):
                if element.attrib.get("rel", "alternate") == "alternate":
                    link = element.attrib.get("href", "")
                    break
        raw_date = (
            (node.findtext(ns + "published") or node.findtext(ns + "updated"))
            if atom
            else node.findtext("pubDate")
        )
        try:
            published = (
                datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if atom
                else parsedate_to_datetime(raw_date)
            )
            if published.tzinfo is None:
                raise ValueError("Undated timezone")
            parts = urlsplit(link)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
            ):
                raise ValueError("Unsafe link")
        except (ValueError, TypeError, AttributeError):
            skipped += 1
            continue
        if not title or not since <= published <= until or link in seen:
            continue
        excerpt = node.findtext(ns + "summary", "") if atom else node.findtext("description", "")
        found.append(
            News(
                id=hashlib.sha256(link.encode()).hexdigest()[:16],
                title=title,
                url=link,
                published_at=published.astimezone(timezone.utc),
                excerpt=clean(excerpt)[:1500],
            )
        )
        seen.add(link)
    return found, skipped


async def collect_news(http, feeds, since, until):
    found, warnings = {}, []
    if not feeds:
        warnings.append("Источники новостей не настроены: анализ новостей недоступен.")
    for index, url in enumerate(feeds, 1):
        try:
            items, skipped = parse_feed(await http.fetch("GET", url), since, until)
            found.update({item.id: item for item in items})
            if skipped:
                warnings.append(f"Лента {index}: пропущено записей без допустимой даты/ссылки: {skipped}.")
        except SourceError as exc:
            warnings.append(f"Лента {index}: {exc}")
    return sorted(found.values(), key=lambda n: n.published_at, reverse=True)[:200], warnings


def relevant_news(items, instrument):
    terms = [instrument.symbol, *instrument.keywords]
    patterns = [re.compile(r"(?<!\w)" + re.escape(t.strip()) + r"(?!\w)", re.I) for t in terms if t.strip()]
    return [n for n in items if any(p.search(n.title + " " + n.excerpt) for p in patterns)][:10]
