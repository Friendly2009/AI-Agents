import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def due_slot(settings, now):
    local = now.astimezone(ZoneInfo(settings.timezone))
    day = local.date() - timedelta(days=(local.weekday() - settings.schedule_weekday) % 7)
    scheduled = datetime(
        day.year,
        day.month,
        day.day,
        settings.schedule_hour,
        settings.schedule_minute,
        tzinfo=ZoneInfo(settings.timezone),
    )
    if local < scheduled:
        return None
    if not settings.schedule_catchup and local.date() != day:
        return None
    return "weekly:" + day.isoformat()


async def scheduler(service):
    while True:
        try:
            slot = due_slot(service.settings, datetime.now(timezone.utc))
            if slot:
                service.submit(slot)
        except Exception as exc:
            logging.getLogger(__name__).error("Scheduler error (%s)", type(exc).__name__)
        await asyncio.sleep(30)
