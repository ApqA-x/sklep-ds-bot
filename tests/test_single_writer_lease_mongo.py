"""Real-Mongo concurrency and publication fence checks for the gateway lease."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pymongo import MongoClient

from stand_guard import guard_db_name, guard_mongo_uri
from voice_tracker import domain, eventlog, supervise

pytestmark = pytest.mark.integration
TEST_MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")


@pytest.fixture()
def db():
    guard_mongo_uri(TEST_MONGO_URI)
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500)
    try:
        client.admin.command("ping")
    except Exception:
        client.close()
        pytest.skip("test mongod is not running")
    name = f"voice_tracker_twriter_{uuid.uuid4().hex[:10]}"
    guard_db_name(name)
    database = client[name]
    yield database
    client.drop_database(name)
    client.close()


def _race_claims(db, count: int = 12):
    barrier = threading.Barrier(count)

    def attempt(index):
        barrier.wait(timeout=5)
        try:
            return supervise.claim_single_writer(db, "gateway", f"host-{index}")
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(attempt, range(count)))


def test_one_winner_for_new_stopped_and_expired_rows(db) -> None:
    first_race = _race_claims(db)
    first = [lease for lease in first_race if lease is not None]
    assert len(first) == 1 and first[0].fence == 1
    assert db[supervise.SINGLE_WRITER_COLLECTION].count_documents({"_id": "gateway"}) == 1

    first[0].release()
    second_race = _race_claims(db)
    second = [lease for lease in second_race if lease is not None]
    assert len(second) == 1 and second[0].fence == 2

    db[supervise.SINGLE_WRITER_COLLECTION].update_one(
        {"_id": "gateway"}, {"$set": {"expiresAt": datetime.now(UTC) - timedelta(seconds=1)}}
    )
    third_race = _race_claims(db)
    third = [lease for lease in third_race if lease is not None]
    assert len(third) == 1 and third[0].fence == 3
    with pytest.raises(supervise.SingleWriterLeaseLost):
        second[0].renew()
    second[0].release()
    third[0].ensure_current()
    assert db[supervise.SINGLE_WRITER_COLLECTION].find_one({"_id": "gateway"})["stopped"] is False


@pytest.mark.asyncio
async def test_superseded_publisher_cannot_change_sequence_or_send(db) -> None:
    class Bus:
        def __init__(self):
            self.sent = []

        async def publish_json(self, subject, value, *, message_id=None):
            self.sent.append((subject, message_id))

    bus = Bus()
    first = supervise.claim_single_writer(db, "gateway", "same-host")
    publisher = eventlog.DurablePublisher(bus, db, issuer="gateway", writer_lease=first)
    payload = {"guildId": "guild", "userId": "user", "eventType": "join"}
    await publisher.publish_json(domain.SUBJECT_VOICE_EVENT, payload)
    seq_doc = db[eventlog.COLL_EVENT_SEQ].find_one({})
    assert seq_doc["seq"] == 1 and len(bus.sent) == 1

    first.release()
    second = supervise.claim_single_writer(db, "gateway", "same-host")
    assert second.fence == 2 and second.owner != first.owner
    with pytest.raises(supervise.SingleWriterLeaseLost):
        await publisher.publish_json(domain.SUBJECT_VOICE_EVENT, payload)
    assert db[eventlog.COLL_EVENT_SEQ].find_one({})["seq"] == 1
    assert len(bus.sent) == 1

    successor = eventlog.DurablePublisher(bus, db, issuer="gateway", writer_lease=second)
    await successor.publish_json(domain.SUBJECT_VOICE_EVENT, payload)
    assert db[eventlog.COLL_EVENT_SEQ].find_one({})["seq"] == 2
    assert len(bus.sent) == 2


@pytest.mark.asyncio
async def test_takeover_after_record_stops_old_writer_before_sequence(db, monkeypatch) -> None:
    class Bus:
        def __init__(self):
            self.sent = []

        async def publish_json(self, subject, value, *, message_id=None):
            self.sent.append(message_id)

    bus = Bus()
    first = supervise.claim_single_writer(db, "gateway", "host-a")
    publisher = eventlog.DurablePublisher(bus, db, issuer="gateway", writer_lease=first)
    real_record = eventlog.record

    def record_then_takeover(*args, **kwargs):
        eid = real_record(*args, **kwargs)
        first.release()
        supervise.claim_single_writer(db, "gateway", "host-b")
        return eid

    monkeypatch.setattr(eventlog, "record", record_then_takeover)
    with pytest.raises(supervise.SingleWriterLeaseLost):
        await publisher.publish_json(
            domain.SUBJECT_VOICE_EVENT, {"guildId": "guild", "userId": "user", "eventType": "join"}
        )
    assert db[eventlog.COLL_EVENT_SEQ].count_documents({}) == 0
    assert bus.sent == []
