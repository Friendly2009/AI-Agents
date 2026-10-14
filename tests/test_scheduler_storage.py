from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from market_agent.scheduler import due_slot
from market_agent.storage import Store

MONDAY = datetime(2026, 10, 12, 6, tzinfo=timezone.utc)


def test_schedule_timezone_catchup(settings):
    assert due_slot(settings, MONDAY - timedelta(seconds=1)) is None
    assert due_slot(settings, MONDAY) == "weekly:2026-10-12"
    assert due_slot(settings, MONDAY + timedelta(days=2)) == "weekly:2026-10-12"
    settings.schedule_catchup = False
    assert due_slot(settings, MONDAY + timedelta(days=2)) is None


def test_atomic_single_run(settings):
    store = Store(settings.database_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: store.claim(now=MONDAY), range(8)))
    assert len([x for x in ids if x]) == 1


def test_scheduled_success_not_repeated(settings):
    store = Store(settings.database_path)
    run = store.claim("weekly:2026-10-12", MONDAY)
    store.finish(run, report={"warnings": []})
    assert store.claim("weekly:2026-10-12", MONDAY + timedelta(hours=1)) is None
    assert Store(settings.database_path).get(run)["status"] == "completed"


def test_failed_schedule_retry_backoff_limit(settings):
    store = Store(settings.database_path)
    for index in range(3):
        now = MONDAY + timedelta(minutes=16 * index)
        run = store.claim("weekly:test", now)
        assert run
        store.finish(run, error="offline")
        assert store.claim("weekly:test", now + timedelta(minutes=1)) is None
    assert store.claim("weekly:test", MONDAY + timedelta(hours=2)) is None


def test_stale_run_recovered(settings):
    store = Store(settings.database_path)
    run = store.claim(now=MONDAY)
    assert store.claim(now=MONDAY + timedelta(minutes=5)) is None
    assert store.claim(now=MONDAY + timedelta(minutes=12))
    assert store.get(run)["status"] == "failed"
