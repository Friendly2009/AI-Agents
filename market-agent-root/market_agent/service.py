import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .analysis import ai_analysis, factual_summary, metrics
from .collector import NewsCollector
from .models import Profile
from .network import SourceError
from .providers import MOEX, Demo, relevant_news, validate_candles
from .recommendations import allocate, barriers, request_signals

log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings, markets, store, http):
        self.settings, self.markets, self.store, self.http = settings, markets, store, http
        self.provider = Demo() if settings.mode == "demo" else MOEX(http)
        self.tasks = set()
        self.collector = NewsCollector(settings, markets, store, http)

    async def build_report(self, now=None, profile_snapshot=None):
        now = now or datetime.now(timezone.utc)
        profile_snapshot = profile_snapshot if profile_snapshot is not None else self.store.profile()
        profile = Profile.model_validate(profile_snapshot["profile"]) if profile_snapshot else None
        end = now.astimezone(ZoneInfo(self.settings.timezone)).date() - timedelta(days=1)
        start = end - timedelta(days=400 if profile else 90)
        warnings = []
        if self.settings.mode == "demo":
            news = []
            warnings.append("ДЕМО: синтетические котировки; реальные новости и модель не запрашиваются.")
        else:
            news_status = await self.collector.refresh(now)
            warnings = list(news_status["warnings"])
            news = self.store.cached_news(
                self.markets.news_feeds, now - timedelta(days=self.settings.news_lookback_days), now
            )
            if not self.settings.llm_enabled:
                warnings.append("ИИ выключен. Выполните настройку Gemini: python -m market_agent setup")
        entries = []
        llm_blocked = None
        for instrument in self.markets.instruments:
            entry = {"instrument": instrument.model_dump(), "key": instrument.key, "warnings": []}
            try:
                candles = validate_candles(await self.provider.candles(instrument, start, end), start, end)
                values = metrics(candles)
                selected = relevant_news(news, instrument)
                entry.update(
                    metrics=values,
                    candles=[c.model_dump(mode="json") for c in candles],
                    news=[n.model_dump(mode="json") for n in selected],
                    analysis_kind="calculated",
                    ai=None,
                )
                if profile and self.settings.mode == "live":
                    try:
                        entry["security"] = await self.provider.security(instrument)
                    except SourceError as exc:
                        entry["warnings"].append(str(exc))
                if (end - candles[-1].day).days > 7:
                    entry["warnings"].append("Котировки старше семи календарных дней.")
                if not selected:
                    entry["warnings"].append("В выбранных лентах не найдено подходящих новостей за период.")
                entry["summary"] = factual_summary(values, len(selected))
                if self.settings.llm_enabled and self.settings.mode == "live" and not profile:
                    try:
                        if llm_blocked:
                            raise SourceError(llm_blocked)
                        entry["ai"] = await ai_analysis(
                            self.http,
                            self.settings,
                            {
                                "instrument": entry["instrument"],
                                "metrics": values,
                                "news": entry["news"],
                                "warnings": warnings + entry["warnings"],
                            },
                        )
                        entry["summary"] = entry["ai"]["summary"]
                        entry["analysis_kind"] = "ai"
                    except SourceError as exc:
                        if exc.status_code in {400, 401, 403, 404, 429, 503}:
                            llm_blocked = str(exc)
                        entry["warnings"].append(f"ИИ недоступен: {exc}. Сохранена расчётная сводка.")
            except SourceError as exc:
                entry["error"] = str(exc)
                entry["warnings"].append(str(exc))
            warnings.extend(f"{instrument.key}: {w}" for w in entry["warnings"])
            entries.append(entry)
        if all("error" in entry for entry in entries):
            raise SourceError("Не удалось получить котировки ни по одному инструменту")
        allocation = None
        if profile:
            eligible = [e for e in entries if not barriers(e, end)]
            if eligible and self.settings.mode == "live" and self.settings.llm_enabled:
                try:
                    signals = await request_signals(self.http, self.settings, profile, eligible)
                    for entry in eligible:
                        entry["ai"] = signals[entry["instrument"]["symbol"]]
                        entry["analysis_kind"] = "ai"
                        entry["summary"] = entry["ai"]["summary"]
                except SourceError as exc:
                    warnings.append(f"Рекомендации ИИ не получены: {exc}")
            else:
                warnings.append("Рекомендации не сформированы: нужен live-режим, ИИ и бумаги с полными данными.")
            allocation = allocate(entries, profile, end, now)
            if not profile.portfolio_known:
                warnings.append("Портфель не указан: количества покупок — модельный сценарий для заданных денег. "
                                "Существующие вложения не учтены; продажи не рассчитываются.")
        return {
            "schema_version": 2,
            "profile": profile_snapshot,
            "allocation": allocation,
            "mode": self.settings.mode,
            "generated_at": now.isoformat(),
            "price_period": {"from": start.isoformat(), "till": end.isoformat()},
            "news_since": (now - timedelta(days=self.settings.news_lookback_days)).isoformat(),
            "warnings": warnings,
            "instruments": entries,
            "news_collection": self.store.news_status(self.markets.news_feeds)
            if self.settings.mode == "live"
            else None,
            "ai_complete": all(e.get("analysis_kind") == "ai" for e in entries),
            "scope": "Обзор и условный план по введённому профилю. Сделки не исполняются. Правила не прошли бэктест.",
        }

    async def execute(self, run_id):
        try:
            report = await asyncio.wait_for(self.build_report(), timeout=600)
            self.store.finish(run_id, report=report)
        except asyncio.CancelledError:
            self.store.finish(run_id, error="Сервис остановлен во время анализа")
            raise
        except SourceError as exc:
            self.store.finish(run_id, error=str(exc))
        except TimeoutError:
            self.store.finish(run_id, error="Превышен лимит анализа: 10 минут")
        except Exception as exc:
            # Do not log upstream payloads, headers, URLs or credentials.
            log.error("Analysis failed (%s)", type(exc).__name__)
            self.store.finish(run_id, error="Внутренняя ошибка анализа; проверьте конфигурацию и журнал")

    def submit(self, slot=None):
        run_id = self.store.claim(slot)
        if run_id:
            task = asyncio.create_task(self.execute(run_id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return run_id

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
