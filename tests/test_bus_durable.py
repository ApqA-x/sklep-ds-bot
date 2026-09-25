"""T09 unit: конверт v1 (schema/freshness), message_id в publish_json,
subscribe(consumer=, db=) → inbox/карантин без сети."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from voice_tracker import eventlog
from voice_tracker.bus import Bus, decode_envelope, sign_envelope

SECRET = b"t09-secret"
SUBJECT = "activity.events"  # issuer обязателен = gateway
ISSUER = "gateway"


class DuplicateKeyError(Exception):
    code = 11000

    def __init__(self) -> None:
        super().__init__("E11000 duplicate key error")


class JCol:
    """Коллекция с уникальным _id и равенственным фильтром — хватает на claim/complete."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    def insert_one(self, doc: dict) -> None:
        if doc["_id"] in self.docs:
            raise DuplicateKeyError()
        self.docs[doc["_id"]] = dict(doc)

    def find_one(self, flt: dict):
        doc = self.docs.get(flt.get("_id"))
        if doc is None:
            return None
        if all(doc.get(k) == v for k, v in flt.items() if k != "_id"):
            return dict(doc)
        return None

    def update_one(self, flt: dict, update: dict) -> SimpleNamespace:
        doc = self.docs.get(flt.get("_id"))
        matched = doc is not None and all(doc.get(k) == v for k, v in flt.items() if k != "_id")
        if matched:
            for key, value in update.get("$set", {}).items():
                doc[key] = value
            for key, value in update.get("$inc", {}).items():
                doc[key] = int(doc.get(key, 0) or 0) + value
        return SimpleNamespace(matched_count=1 if matched else 0, modified_count=1 if matched else 0)

    def count_documents(self, flt: dict) -> int:
        return sum(1 for d in self.docs.values() if all(d.get(k) == v for k, v in flt.items()))


class JDb:
    def __init__(self) -> None:
        self._c: dict[str, JCol] = {}

    def __getitem__(self, name: str) -> JCol:
        return self._c.setdefault(name, JCol())


class FakeConn:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.cb = None

    def publish(self, subject: str, body: bytes) -> None:
        self.published.append((subject, body))

    def subscribe(self, subject: str, *, cb) -> object:
        self.cb = cb
        return object()


def _envelope_body(message_id: str, payload: dict, *, issued_at: int | None = None, schema: int = 1, secret: bytes = SECRET) -> bytes:
    ts = issued_at if issued_at is not None else int(datetime.now(UTC).timestamp())
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    env = {
        "v": schema,
        "messageId": message_id,
        "subject": SUBJECT,
        "issuer": ISSUER,
        "issuedAt": ts,
        "payload": payload,
        "signature": sign_envelope(secret, message_id, SUBJECT, ISSUER, ts, raw),
    }
    return json.dumps(env, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_publish_json_passes_message_id_through() -> None:
    conn = FakeConn()
    bus = Bus(conn, SECRET, ISSUER)

    async def run() -> None:
        await bus.publish_json(SUBJECT, {"a": 1}, message_id="stable-id-1")
        await bus.publish_json(SUBJECT, {"a": 2})
        await bus.publish_json(SUBJECT, {"a": 3})

    asyncio.run(run())

    first = json.loads(conn.published[0][1])
    assert first["messageId"] == "stable-id-1"
    assert first["v"] == 1
    second = json.loads(conn.published[1][1])
    third = json.loads(conn.published[2][1])
    assert second["messageId"] != third["messageId"]  # uuid по умолчанию уникален
    # подпись стабильного id верифицируется тем же ключом
    env, _ = decode_envelope(SECRET, SUBJECT, conn.published[0][1])
    assert env.message_id == "stable-id-1"


def test_decode_rejects_unknown_schema() -> None:
    body = _envelope_body("m1", {"x": 1}, schema=2)
    with pytest.raises(ValueError, match="unsupported envelope schema"):
        decode_envelope(SECRET, SUBJECT, body)


def test_max_age_window_is_configurable() -> None:
    old_ts = int((datetime.now(UTC) - timedelta(seconds=120)).timestamp())
    body = _envelope_body("m-old", {"x": 1}, issued_at=old_ts)
    with pytest.raises(ValueError, match="stale envelope"):
        decode_envelope(SECRET, SUBJECT, body, max_age_seconds=30)
    env, _ = decode_envelope(SECRET, SUBJECT, body, max_age_seconds=3600)
    assert env.message_id == "m-old"


def test_decode_rejects_bad_signature() -> None:
    body = _envelope_body("m-bad", {"x": 1}, secret=b"other-key")
    with pytest.raises(ValueError, match="invalid signature"):
        decode_envelope(SECRET, SUBJECT, body)


@pytest.mark.asyncio
async def test_subscribe_consumer_routes_into_inbox_once() -> None:
    conn = FakeConn()
    bus = Bus(conn, SECRET, ISSUER)
    db = JDb()
    seen: list[bytes] = []

    async def handler(payload: bytes) -> None:
        seen.append(payload)

    await bus.subscribe(None, SUBJECT, None, handler, consumer="c1", db=db)
    body = _envelope_body("evt-1", {"x": 1})
    await conn.cb(SimpleNamespace(data=body))
    await conn.cb(SimpleNamespace(data=body))  # повторная wire-доставка

    assert len(seen) == 1
    iid = eventlog.inbox_id("evt-1", "c1")
    assert db["event_inbox"].docs[iid]["state"] == eventlog.STATE_COMPLETED
    assert db["event_inbox"].docs[iid]["consumer"] == "c1"


@pytest.mark.asyncio
async def test_subscribe_poison_quarantines_without_handler() -> None:
    conn = FakeConn()
    bus = Bus(conn, SECRET, ISSUER)
    db = JDb()
    calls: list[bytes] = []

    async def handler(payload: bytes) -> None:
        calls.append(payload)

    await bus.subscribe(None, SUBJECT, None, handler, consumer="c1", db=db)
    await conn.cb(SimpleNamespace(data=_envelope_body("evt-x", {"x": 1}, secret=b"wrong")))
    await conn.cb(SimpleNamespace(data=b"not-json-at-all"))

    assert calls == []
    quarantined = [d for d in db["event_inbox"].docs.values() if d["state"] == eventlog.STATE_QUARANTINED]
    assert len(quarantined) == 2  # оба яда переживают повтор: quarantine идемпотентен по хэшу
    await conn.cb(SimpleNamespace(data=b"not-json-at-all"))
    quarantined2 = [d for d in db["event_inbox"].docs.values() if d["state"] == eventlog.STATE_QUARANTINED]
    assert len(quarantined2) == 2
