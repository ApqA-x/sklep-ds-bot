"""T09 интеграция: outbox/inbox на реальной Mongo стенда (порт 27099).

E01 — пропуск wire догружается sweep'ом в порядке createdAt;
E02/T09.4 — сбой транспорта не теряет событие, republish тем же message_id;
E03 — сбой handler'а освобождает lease для повторной попытки;
E04 — детерминированный id для событий из устойчивого факта (session.closed);
E05 — inbox per-consumer: один consumer не «съедает» событие другого;
E06 — гонка wire-доставки и sweep'а исполняет handler ровно один раз;
E08 — исчерпание max_deliver → карантин с причиной.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from voice_tracker import domain, eventlog  # noqa: E402
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


class Bus:
    """Транспорт-заглушка: пишет published-вызовы, может падать первые N раз."""

    def __init__(self, fail_times: int = 0) -> None:
        self.published: list[tuple[str, str]] = []  # (subject, message_id)
        self.values: list[object] = []
        self._fail = fail_times

    async def publish_json(self, subject: str, value: object, *, message_id: str | None = None) -> None:
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("nats transport down")
        self.published.append((subject, message_id or ""))
        self.values.append(value)


@pytest.fixture()
def db():
    guard_mongo_uri(TEST_MONGO_URI)
    if not _server_up():
        pytest.skip("test mongod is not running on %s" % TEST_MONGO_URI)
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=3000)
    name = f"voice_tracker_t09ev_{uuid.uuid4().hex[:10]}"
    guard_db_name(name)
    database = client[name]
    yield database
    client.drop_database(name)
    client.close()


# ------------------------------------------------------------------ E02 / T09.4


@pytest.mark.asyncio
async def test_publish_durable_survives_transport_failure_and_republishes_same_id(db) -> None:
    bus = Bus(fail_times=1)
    eid = await eventlog.publish_durable(
        bus, db, domain.SUBJECT_ACTIVITY_EVENT, {"event_type": "join", "guild_id": "g1"}
    )
    # журнал устойчив, транспорт упал → publishedAt=None + причина
    row = db[eventlog.COLL_EVENT_LOG].find_one({"_id": eid})
    assert row is not None and row["publishedAt"] is None
    assert "transport down" in row["publishError"]
    assert bus.published == []

    n = await eventlog.republish_pending(bus, db, domain.SUBJECT_ACTIVITY_EVENT)
    assert n == 1
    # T09.4: повтор — тот же message_id, потребители дедуплицируют по нему (E02)
    assert bus.published == [(domain.SUBJECT_ACTIVITY_EVENT, eid)]
    row = db[eventlog.COLL_EVENT_LOG].find_one({"_id": eid})
    assert row["publishedAt"] is not None and row["publishError"] is None

    # повторный republish ничего не находит — событие подтверждено
    assert await eventlog.republish_pending(bus, db, domain.SUBJECT_ACTIVITY_EVENT) == 0


# ---------------------------------------------------------------------- E04


@pytest.mark.asyncio
async def test_durable_publisher_deterministic_id_for_session_closed(db) -> None:
    bus = Bus()
    publisher = eventlog.DurablePublisher(bus, db, issuer="tracker")
    payload = {"sessionId": "s-42", "guildId": "g1"}
    await publisher.publish_json(domain.SUBJECT_SESSION_CLOSED, payload)
    await publisher.publish_json(domain.SUBJECT_SESSION_CLOSED, payload)  # reaper/tracker дважды

    expected = eventlog.deterministic_event_id("session-closed", "s-42")
    assert [m for _, m in bus.published] == [expected, expected]
    assert db[eventlog.COLL_EVENT_LOG].count_documents({}) == 1  # один факт — одна строка журнала


# ---------------------------------------------------------------------- E05


@pytest.mark.asyncio
async def test_inbox_is_per_consumer(db) -> None:
    eid = eventlog.record(db, domain.SUBJECT_VOICE_EVENT, {"user": "u1"})
    seen: list[str] = []

    async def handler_a(payload: bytes) -> None:
        seen.append("a")

    async def handler_b(payload: bytes) -> None:
        seen.append("b")

    assert await eventlog.deliver(db, "tracker", eid, domain.SUBJECT_VOICE_EVENT, b"{}", handler_a) == "completed"
    # consumer='tracker' завершил — consumer='stalker' всё равно получает своё
    assert await eventlog.deliver(db, "stalker", eid, domain.SUBJECT_VOICE_EVENT, b"{}", handler_b) == "completed"
    # повтор тому же consumer'у — skip, handler не повторяется
    assert await eventlog.deliver(db, "tracker", eid, domain.SUBJECT_VOICE_EVENT, b"{}", handler_a) == "skipped"
    assert seen == ["a", "b"]


# ---------------------------------------------------------------------- E03/E08


@pytest.mark.asyncio
async def test_handler_failure_retries_then_quarantines(db) -> None:
    eid = eventlog.record(db, domain.SUBJECT_ACTIVITY_EVENT, {"event_type": "x"})
    attempts: list[int] = []

    async def failing(payload: bytes) -> None:
        attempts.append(len(attempts) + 1)
        raise RuntimeError("db write failed")

    outcome = await eventlog.deliver(db, "activity", eid, domain.SUBJECT_ACTIVITY_EVENT, b"{}", failing, max_deliver=3)
    assert outcome == eventlog.STATE_RECEIVED  # E03: transient → обратно в received
    doc = db[eventlog.COLL_EVENT_INBOX].find_one({"_id": eventlog.inbox_id(eid, "activity")})
    assert doc["state"] == eventlog.STATE_RECEIVED and doc["attempts"] == 1
    assert "db write failed" in doc["lastError"]

    outcome = await eventlog.deliver(db, "activity", eid, domain.SUBJECT_ACTIVITY_EVENT, b"{}", failing, max_deliver=3)
    assert outcome == eventlog.STATE_RECEIVED
    outcome = await eventlog.deliver(db, "activity", eid, domain.SUBJECT_ACTIVITY_EVENT, b"{}", failing, max_deliver=3)
    assert outcome == eventlog.STATE_QUARANTINED  # E08: 3-я попытка — карантин с причиной
    doc = db[eventlog.COLL_EVENT_INBOX].find_one({"_id": eventlog.inbox_id(eid, "activity")})
    assert doc["state"] == eventlog.STATE_QUARANTINED and doc["attempts"] == 3
    # карантинное событие больше не исполняется
    assert await eventlog.deliver(db, "activity", eid, domain.SUBJECT_ACTIVITY_EVENT, b"{}", failing) == "skipped"
    assert len(attempts) == 3


# ------------------------------------------------------- lease / fence (T09.6)


@pytest.mark.asyncio
async def test_expired_lease_takeover_stale_ack_rejected(db) -> None:
    eid = eventlog.record(db, domain.SUBJECT_SESSION_CLOSED, {"session_id": "s9"})
    first = eventlog.claim(db, eid, "writer", domain.SUBJECT_SESSION_CLOSED)
    assert first is not None
    # активный lease → никто второй не исполняет
    assert eventlog.claim(db, eid, "writer", domain.SUBJECT_SESSION_CLOSED) is None

    # lease истёк (симуляция зависшего исполнителя) → CAS-перехват
    db[eventlog.COLL_EVENT_INBOX].update_one(
        {"_id": first.inbox_id},
        {"$set": {"leaseExpiresAt": datetime.now(UTC) - timedelta(seconds=1)}},
    )
    second = eventlog.claim(db, eid, "writer", domain.SUBJECT_SESSION_CLOSED)
    assert second is not None and second.attempts == 2 and second.lease_token != first.lease_token
    # «мёртвый» исполнитель не может смаркировать completed (fence)
    assert eventlog.complete(db, first) is False
    assert eventlog.complete(db, second) is True
    assert await eventlog.deliver(db, "writer", eid, domain.SUBJECT_SESSION_CLOSED, b"{}", _noop_handler) == "skipped"


async def _noop_handler(_payload: bytes) -> None:
    return None


# ------------------------------------------------------------------ E01/E06


@pytest.mark.asyncio
async def test_sweep_delivers_in_chronological_order(db) -> None:
    import json

    now = datetime.now(UTC)
    # вставка в обратном порядке времени: sweep обязан отсортировать по createdAt
    for i in (2, 1, 0):
        db[eventlog.COLL_EVENT_LOG].insert_one(
            {
                "_id": f"evt-{i}",
                "subject": domain.SUBJECT_VOICE_EVENT,
                "issuer": "gateway",
                "payload": {"seq": i},
                "createdAt": now + timedelta(milliseconds=10 * i),
                "publishedAt": now,
                "publishError": None,
            }
        )

    order: list[int] = []

    async def handler(payload: bytes) -> None:
        order.append(json.loads(payload)["seq"])

    n = await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler, max_deliver=8)
    assert n == 3
    assert order == [0, 1, 2]  # E01: порядок восстановления chronological

    # повторный sweep ничего не исполняет: всё completed
    assert await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler) == 0


@pytest.mark.asyncio
async def test_wire_and_sweep_share_single_delivery_point(db) -> None:
    # E06: событие, уже доставленное по wire, не получает второго эффекта от sweep'а,
    # и наоборот — обе дорожки входят в один claim/CAS.
    eid = eventlog.record(db, domain.SUBJECT_VOICE_EVENT, {"user": "u"})
    calls: list[str] = []

    async def handler(_payload: bytes) -> None:
        calls.append("effect")

    outcome = await eventlog.deliver(db, "tracker", eid, domain.SUBJECT_VOICE_EVENT, b"{}", handler)
    assert outcome == "completed"
    assert await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler) == 0
    assert calls == ["effect"]  # ровно один эффект на событие

    # обратный порядок: sweep догнал, wire-повтор пропускается
    eid2 = eventlog.record(db, domain.SUBJECT_VOICE_EVENT, {"user": "v"})
    assert await eventlog.sweep_pending(db, "tracker", [domain.SUBJECT_VOICE_EVENT], handler) == 1
    assert await eventlog.deliver(db, "tracker", eid2, domain.SUBJECT_VOICE_EVENT, b"{}", handler) == "skipped"
    assert calls == ["effect", "effect"]  # два события — два эффекта, без дублей


@pytest.mark.asyncio
async def test_pending_stats_reports_backlog_and_processed(db) -> None:
    eid = eventlog.record(db, domain.SUBJECT_VOICE_EVENT, {"user": "u"})
    stats = eventlog.pending_stats(db, "tracker", [domain.SUBJECT_VOICE_EVENT])
    assert stats["backlog"] == 1 and stats["processed"] == 0

    await eventlog.deliver(db, "tracker", eid, domain.SUBJECT_VOICE_EVENT, b"{}", _noop_handler)
    stats = eventlog.pending_stats(db, "tracker", [domain.SUBJECT_VOICE_EVENT])
    assert stats["backlog"] == 0 and stats["processed"] == 1 and stats["quarantined"] == 0


# ---------------------------------------------------------------- record/dedup


@pytest.mark.asyncio
async def test_record_duplicate_idempotent(db) -> None:
    eid = eventlog.record(db, domain.SUBJECT_ACTIVITY_EVENT, {"event_type": "x"}, event_id="fixed-id")
    again = eventlog.record(db, domain.SUBJECT_ACTIVITY_EVENT, {"event_type": "x"}, event_id="fixed-id")
    assert again == eid
    assert db[eventlog.COLL_EVENT_LOG].count_documents({}) == 1
