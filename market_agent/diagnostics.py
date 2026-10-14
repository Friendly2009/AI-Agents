from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .analysis import ai_analysis
from .network import SourceError
from .providers import MOEX


async def diagnose(service, check_llm=True):
    settings = service.settings
    now = datetime.now(timezone.utc)
    checks = []
    if settings.mode != "live":
        return {
            "checked_at": now.isoformat(),
            "ready": False,
            "checks": [
                {
                    "name": "Режим",
                    "ok": False,
                    "detail": "Включён demo. Выполните python -m market_agent setup.",
                }
            ],
        }
    end = now.astimezone(ZoneInfo(settings.timezone)).date() - timedelta(days=1)
    provider = MOEX(service.http)
    for instrument in service.markets.instruments:
        try:
            candles = await provider.candles(instrument, end - timedelta(days=30), end)
            fresh = (end - candles[-1].day).days <= 7
            checks.append(
                {
                    "name": instrument.key,
                    "ok": fresh,
                    "detail": f"Свечей: {len(candles)}. Последняя: {candles[-1].day}. Цена: {candles[-1].close}.",
                }
            )
        except SourceError as exc:
            checks.append({"name": instrument.key, "ok": False, "detail": str(exc)})
    status = await service.collector.refresh(now, force=True)
    items = service.store.cached_news(
        service.markets.news_feeds, now - timedelta(days=settings.news_lookback_days), now
    )
    checks.append(
        {
            "name": "Новости",
            "ok": bool(items) and not status["warnings"],
            "detail": f"Новостей за период в базе: {len(items)}. " + " ".join(status["warnings"]),
        }
    )
    if check_llm:
        if not settings.llm_enabled:
            checks.append(
                {
                    "name": "Gemini",
                    "ok": False,
                    "detail": "ИИ выключен. Выполните python -m market_agent setup.",
                }
            )
        else:
            try:
                await ai_analysis(
                    service.http,
                    settings,
                    {
                        "task": "Проверка соединения: кратко сообщите, что данных для рыночного анализа нет.",
                        "instrument": None,
                        "metrics": {},
                        "news": [],
                        "warnings": ["Это диагностика, не рыночный отчёт."],
                    },
                )
                checks.append(
                    {
                        "name": "Gemini",
                        "ok": True,
                        "detail": f"{settings.llm_model}: получен валидный JSON-ответ. Тариф проверяйте в AI Studio.",
                    }
                )
            except SourceError as exc:
                checks.append({"name": "Gemini", "ok": False, "detail": str(exc)})
    return {
        "checked_at": now.isoformat(),
        "ready": bool(checks) and all(c["ok"] for c in checks),
        "checks": checks,
    }
