import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .providers import collect_news


class NewsCollector:
    def __init__(self, settings, markets, store, http):
        self.settings, self.markets, self.store, self.http = settings, markets, store, http
        self.lock = asyncio.Lock()

    async def refresh(self, now=None, force=False):
        now = now or datetime.now(timezone.utc)
        async with self.lock:
            status = self.store.news_status(self.markets.news_feeds)
            previous = status["last_checked_at"]
            if not force and previous and now - datetime.fromisoformat(previous) < timedelta(minutes=2):
                return status
            items, warnings = await collect_news(
                self.http,
                self.markets.news_feeds,
                now - timedelta(days=self.settings.news_lookback_days),
                now,
            )
            self.store.cache_news(items, self.markets.news_feeds, now, warnings)
            return self.store.news_status(self.markets.news_feeds)

    async def loop(self):
        while True:
            try:
                await asyncio.wait_for(self.refresh(), timeout=240)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).error("News collection failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.settings.news_poll_minutes * 60)
