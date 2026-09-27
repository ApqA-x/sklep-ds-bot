"""R26-01 regression: полный sweep журнала, курсор consumer'а и честный backlog.

Смысловые сценарии ревью 26.09 (bug_present диагностировал дефект — здесь
ожидается ИСПРАВЛЕННОЕ поведение):
  V26-02 — 201+ событий: первая страница не блокирует остальные; последовательные
           sweeps обязаны дойти до конца; одна доставка на событие;
  V26-03 — retry старых событий не исчезает за high-water mark (inbox-путь),
           смешанные состояния completed/quarantined/active/expired/received;
  V26-04 — pending-статистика отражает реальные состояния (received/expired не
           теряются), active processing и карантин — отдельно.
Плюс: новый consumer без курсора, крах до сохранения чекпойнта, опоздавшая
вставка (gap-проход), повреждённый курсор, bounded-стоимость тика, индексы M5.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from voice_tracker import domain, eventlog, migrate, schema  # noqa: E402
from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402

pytestmark = pytest.mark.integration

TEST_MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")


def _server_up() -> bool:
    try:
        client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    guard_mongo_uri(TEST_MONGO_URI)
    if not _server_up():
        pytest.skip("test mongod is not running on %s" % TEST_MONGO_URI)
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=3000)
    name = f"voice_tracker_t26r01_{uuid.uuid4().hex[:10]}"
    guard_db_name(name)
    database = client[name]
    yield database
    client.drop_database(name)
    client.close()


def _insert_events(db, n: int, *, start: datetime | None = None) -> list[str]:
    """n событий журнала в порядке вставки (createdAt равный/возрастающий)."""
    base = start or datetime.now(UTC)
    ids = []
    for i in range(n):
        eid = f"r2601-{i:06d}"
        ids.append(eid)
        db[eventlog.COLL_EVENT_LOG].insert_one(
            {
                "_id": eid,
                "subject": domain.SUBJECT_VOICE_EVENT,
                "issuer": "gateway",
                "payload": {"seq": i},
                "createdAt": base + timedelta(milliseconds=i),
                "publishedAt": base,
                "publishError": None,
            }
        )
    return ids


# ------------------------------------------------------------------ V26-02


@pytest.mark.asyncio
async def test_sweep_drains_beyond_first_page_201(db) -> None:
    """Раньше: 201-е событие оставалось недоставленным навсегда (первые 200
    terminal-строк съедали страницу, курсора не было)."""
    ids = _insert_events(db, 201)
    seen: list[int] = []

    async def handler(payload: bytes) -> None:
        import json

        seen.append(json.loads(payload)["seq"])

    total = 0
    for _ in range(10):  # тики до нуля; лимит тика заведомо меньше журнала
        n = await eventlog.sweep_pending(
            db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler, limit=50
        )
        total += n
        if n == 0:
            break
    assert total == 201
    assert sorted(seen) == list(range(201))
    # порядок восстановления chronological и ровно одна доставка на событие
    assert seen == sorted(seen)
    # further ticks deliver nothing
    assert await eventlog.sweep_pending(
        db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler, limit=50
    ) == 0


@pytest.mark.asyncio
async def test_sweep_drains_1000_events_with_bounded_tick(db) -> None:
    """≥1000 событий: каждая страница ограничена limit, полное время — конечные
    тики; ни одно событие не потеряно (V26-02)."""
    _insert_events(db, 1000)
    seen: set[int] = set()

    async def handler(payload: bytes) -> None:
        import json

        seen.add(json.loads(payload)["seq"])

    ticks = 0
    per_tick = []
    while True:
        n = await eventlog.sweep_pending(
            db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler, limit=200
        )
        ticks += 1
        per_tick.append(n)
        if n == 0:
            break
        assert ticks < 30, "sweep не сходится"
    assert seen == set(range(1000))
    assert max(per_tick) <= 200  # стоимость тика ограничена


@pytest.mark.asyncio
async def test_new_consumer_without_checkpoint_replays_entire_journal(db) -> None:
    _insert_events(db, 7)
    seen: set[int] = set()

    async def handler(payload: bytes) -> None:
        import json

        seen.add(json.loads(payload)["seq"])

    n = await eventlog.sweep_pending(db, "fresh", [domain.SUBJECT_VOICE_EVENT], handler)
    assert n == 7
    assert seen == set(range(7))


# ------------------------------------------------------------------ V26-03


@pytest.mark.asyncio
async def test_mixed_inbox_states_sweep(db) -> None:
    """completed/quarantined пропускаются, активный lease не трогается, истёкший —
    перехватывается, received — повторяется (V26-03)."""
    ids = _insert_events(db, 5)
    consumer = "tracker"
    now = datetime.now(UTC)

    def mk_inbox(eid: str, state: str, **extra) -> None:
        doc = {
            "_id": eventlog.inbox_id(eid, consumer),
            "eventId": eid,
            "consumer": consumer,
            "subject": domain.SUBJECT_VOICE_EVENT,
            "state": state,
            "attempts": 1,
            "leaseToken": uuid.uuid4().hex,
            "leaseExpiresAt": now + timedelta(seconds=120),
            "lastError": None,
            "createdAt": now,
            "updatedAt": now,
            "completedAt": None,
        }
        doc.update(extra)
        db[eventlog.COLL_EVENT_INBOX].insert_one(doc)

    mk_inbox(ids[0], eventlog.STATE_COMPLETED)
    mk_inbox(ids[1], eventlog.STATE_QUARANTINED)
    mk_inbox(ids[2], eventlog.STATE_PROCESSING)  # активный lease
    mk_inbox(
        ids[3],
        eventlog.STATE_PROCESSING,
        leaseExpiresAt=now - timedelta(seconds=10),  # истёкший
    )
    mk_inbox(ids[4], eventlog.STATE_RECEIVED, leaseToken=None, leaseExpiresAt=None)

    seen: set[int] = set()

    async def handler(payload: bytes) -> None:
        import json

        seen.add(json.loads(payload)["seq"])

    n = await eventlog.sweep_pending(db, consumer, [domain.SUBJECT_VOICE_EVENT], handler)
    # ids[2] активный — не трогаем; остальные новые для delivery: ids[3], ids[4]
    # + forward доставляет ids[3], ids[4] (received/expired обрабатывает retry-проход)
    assert seen == {3, 4}
    assert n == 2


@pytest.mark.asyncio
async def test_retry_of_old_event_survives_high_water_mark(db) -> None:
    """Событие упало, курсор ушёл дальше; повторная попытка обязана дойти до него
    через inbox-путь, а не застрять за high-water mark (R26-01.2)."""
    ids = _insert_events(db, 3)
    failures = {"on": True}

    async def flaky(payload: bytes) -> None:
        import json

        if failures["on"] and json.loads(payload)["seq"] == 0:
            raise RuntimeError("transient")

    n = await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], flaky, limit=10)
    assert n == 3  # попыток исполнено 3 (одно упало)
    doc = db[eventlog.COLL_EVENT_INBOX].find_one(
        {"_id": eventlog.inbox_id(ids[0], "tracker")}
    )
    assert doc["state"] == eventlog.STATE_RECEIVED

    # курсор уже за всеми 3 событиями; включаем успех и ждём retry-проход
    failures["on"] = False
    cursor = db[eventlog.COLL_SWEEP_STATE].find_one(
        {"_id": eventlog._state_key("tracker", [domain.SUBJECT_VOICE_EVENT])}
    )
    assert cursor is not None and cursor["position"]["eventId"] == ids[2]

    n2 = await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], flaky, limit=10)
    assert n2 == 1
    doc = db[eventlog.COLL_EVENT_INBOX].find_one(
        {"_id": eventlog.inbox_id(ids[0], "tracker")}
    )
    assert doc["state"] == eventlog.STATE_COMPLETED


@pytest.mark.asyncio
async def test_crash_before_checkpoint_no_duplicate_effects(db) -> None:
    """Рестарт до сохранения чекпойнта: повторный sweep не повторяет эффекты
    (inbox идемпотентность), курсор восстанавливается."""
    ids = _insert_events(db, 6)
    seen: list[int] = []

    async def handler(payload: bytes) -> None:
        import json

        seen.append(json.loads(payload)["seq"])

    await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler, limit=3)
    assert len(seen) == 3
    # «крах до checkpoint»: удаляем состояние — курсор обнулён, эффекты нет
    db[eventlog.COLL_SWEEP_STATE].delete_many({})
    n = await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler, limit=10)
    assert n == 3  # только недоставленные 3, без повторов
    assert sorted(seen) == list(range(6))


@pytest.mark.asyncio
async def test_backdated_insert_caught_by_gap_pass(db) -> None:
    """Вставка с «опоздавшим» createdAt за курсор — forward её не увидит;
    gap-проход обязан догрузить (R26-01.4)."""
    base = datetime.now(UTC)
    ids = _insert_events(db, 2, start=base)
    seen: set[int] = set()

    async def handler(payload: bytes) -> None:
        import json

        seen.add(json.loads(payload)["seq"])

    await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler)
    assert seen == {0, 1}

    # опоздавшая вставка: createdAt на 30 c раньше обеих (за курсором)
    db[eventlog.COLL_EVENT_LOG].insert_one(
        {
            "_id": "r2601-late",
            "subject": domain.SUBJECT_VOICE_EVENT,
            "issuer": "gateway",
            "payload": {"seq": 99},
            "createdAt": base - timedelta(seconds=30),
            "publishedAt": base,
            "publishError": None,
        }
    )
    n = await eventlog.sweep_pending(
        db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler, gap_seconds=120
    )
    assert n == 1 and seen == {0, 1, 99}


@pytest.mark.asyncio
async def test_corrupted_checkpoint_full_replay_no_crash(db) -> None:
    _insert_events(db, 4)
    key = eventlog._state_key("tracker", [domain.SUBJECT_VOICE_EVENT])
    db[eventlog.COLL_SWEEP_STATE].insert_one(
        {"_id": key, "position": {"createdAt": "не дата", "eventId": 12345}, "updatedAt": datetime.now(UTC)}
    )
    seen: set[int] = set()

    async def handler(payload: bytes) -> None:
        import json

        seen.add(json.loads(payload)["seq"])

    n = await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler)
    assert n == 4  # повреждённый курсор = replay с начала, не падение
    # и курсор починился записью позиции
    doc = db[eventlog.COLL_SWEEP_STATE].find_one({"_id": key})
    assert isinstance(doc["position"]["eventId"], str)


# ------------------------------------------------------------------ V26-04


@pytest.mark.asyncio
async def test_stats_report_real_pending_states(db) -> None:
    """received и истёкший processing — backlog; активные попытки и карантин —
    отдельные поля (раньше pending считался только при отсутствии inbox)."""
    ids = _insert_events(db, 6)
    consumer = "tracker"
    now = datetime.now(UTC)

    def mk(eid: str, state: str, lease=None) -> None:
        db[eventlog.COLL_EVENT_INBOX].insert_one(
            {
                "_id": eventlog.inbox_id(eid, consumer),
                "eventId": eid,
                "consumer": consumer,
                "subject": domain.SUBJECT_VOICE_EVENT,
                "state": state,
                "attempts": 1,
                "leaseToken": "t" if state == eventlog.STATE_PROCESSING else None,
                "leaseExpiresAt": lease,
                "lastError": None,
                "createdAt": now - timedelta(seconds=100),
                "updatedAt": now,
                "completedAt": None,
            }
        )

    mk(ids[0], eventlog.STATE_COMPLETED)
    mk(ids[1], eventlog.STATE_QUARANTINED)
    mk(ids[2], eventlog.STATE_PROCESSING, lease=now + timedelta(seconds=60))  # active
    mk(ids[3], eventlog.STATE_PROCESSING, lease=now - timedelta(seconds=5))  # expired
    mk(ids[4], eventlog.STATE_RECEIVED)  # retryable
    # ids[5] — без inbox: missing

    stats = eventlog.pending_stats(db, consumer, [domain.SUBJECT_VOICE_EVENT])
    assert stats["missingInbox"] == 1
    assert stats["received"] == 1
    assert stats["expiredProcessing"] == 1
    assert stats["activeProcessing"] == 1
    assert stats["quarantined"] == 1
    assert stats["processed"] == 1
    assert stats["backlog"] == 3  # missing + received + expired
    assert stats["oldestPendingAgeSeconds"] >= 100  # oldest относится к реальной работе


@pytest.mark.asyncio
async def test_stats_no_full_scan_cost(db) -> None:
    """R26-01.6: метрика — несколько indexed-запросов, не полный N+1: на журнале
    1500 строк (потолок cap=1000 в тесте) стоимость ограничена и не равна числу строк."""
    _insert_events(db, 1500)
    stats = eventlog._pending_stats_sync(
        db, "metrics", [domain.SUBJECT_VOICE_EVENT], cap=1000
    )
    assert stats["missingInbox"] == 1000  # ограничено потолком, не 1500
    assert stats["missingCapped"] is True


# ------------------------------------------------------- M5 индексы + explain


def test_m5_creates_sweep_indexes_and_idempotent(db) -> None:
    result = migrate.plan_and_apply(db, apply=True, only=5)
    actions = [a["action"] for a in result["actions"]]
    assert actions == ["applied"]
    names_el = {i["name"] for i in db[schema.EL].list_indexes()}
    names_ei = {i["name"] for i in db[schema.EI].list_indexes()}
    assert "event_log_subject_createdAt_id" in names_el
    assert "event_inbox_consumer_state_createdAt" in names_ei
    again = migrate.plan_and_apply(db, apply=True, only=5)
    assert [a["action"] for a in again["actions"]] == ["skip-done"]


async def _noop(_payload: bytes) -> None:
    return None


@pytest.mark.asyncio
async def test_forward_scan_uses_index(db) -> None:
    """explain: forward-выборка за курсором — IXSCAN по M5-индексу (иначе на
    растущем журнале in-memory sort всего хвоста)."""
    migrate.plan_and_apply(db, apply=True, only=5)
    ids = _insert_events(db, 10)
    await eventlog.sweep_pending(
        db, "tracker", [domain.SUBJECT_VOICE_EVENT], _noop, limit=1
    )
    position = eventlog._load_position(
        db, eventlog._state_key("tracker", [domain.SUBJECT_VOICE_EVENT])
    )
    assert position is not None
    explain = db[eventlog.COLL_EVENT_LOG].find(
        eventlog._forward_filter([domain.SUBJECT_VOICE_EVENT], position)
    ).sort([("createdAt", 1), ("_id", 1)]).limit(100).explain()
    stages = explain.get("queryPlanner", {}).get("winningPlan", {})
    text = str(stages)
    assert "event_log_subject_createdAt_id" in text or "IXSCAN" in text
