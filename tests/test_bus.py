from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from voice_tracker.bus import Bus, decode_envelope, issuer_for_subject, sign_envelope


class FakeConnection:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: dict[str, callable] = {}

    def publish(self, subject: str, body: bytes):
        self.published.append((subject, body))

    def subscribe(self, subject: str, cb=None):
        self.subscriptions[subject] = cb
        return object()


class FakeDeduper:
    def __init__(self, claim_result: bool = True) -> None:
        self.claim_result = claim_result
        self.calls: list[tuple[str, str, str, int]] = []

    def claim_message(self, _ctx, subject: str, message_id: str, issuer: str, issued_at: int) -> bool:
        self.calls.append((subject, message_id, issuer, issued_at))
        return self.claim_result


def _make_envelope(secret: str, subject: str, payload: dict[str, object]) -> bytes:
    payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    message_id = "msg-1"
    issued_at = int(datetime.now(UTC).timestamp())
    signature = sign_envelope(secret, message_id, subject, issuer_for_subject(subject), issued_at, payload_bytes)
    body = {
        "messageId": message_id,
        "subject": subject,
        "issuer": issuer_for_subject(subject),
        "issuedAt": issued_at,
        "payload": payload,
        "signature": signature,
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_publish_and_decode_round_trip() -> None:
    conn = FakeConnection()
    bus = Bus(conn, "secret", "gateway")

    asyncio.run(bus.publish_json(None, "voice.events", {"hello": "world"}))

    subject, body = conn.published[0]
    assert subject == "voice.events"
    env, payload = decode_envelope("secret", "voice.events", body)
    assert env.subject == "voice.events"
    assert payload == b'{"hello":"world"}'


def test_subscribe_uses_deduper_and_handler() -> None:
    conn = FakeConnection()
    bus = Bus(conn, "secret", "gateway")
    deduper = FakeDeduper()
    received: list[bytes] = []

    async def handler(payload: bytes) -> None:
        received.append(payload)

    asyncio.run(bus.subscribe(None, "voice.events", deduper, handler))
    message = _make_envelope("secret", "voice.events", {"hello": "world"})
    asyncio.run(conn.subscriptions["voice.events"](type("Msg", (), {"data": message})()))

    assert deduper.calls
    assert received == [b'{"hello":"world"}']


def test_subscribe_drops_duplicates_without_deduper() -> None:
    conn = FakeConnection()
    bus = Bus(conn, "secret", "gateway")
    received: list[bytes] = []

    async def handler(payload: bytes) -> None:
        received.append(payload)

    asyncio.run(bus.subscribe(None, "voice.events", None, handler))
    message = _make_envelope("secret", "voice.events", {"hello": "world"})
    callback = conn.subscriptions["voice.events"]
    asyncio.run(callback(type("Msg", (), {"data": message})()))
    asyncio.run(callback(type("Msg", (), {"data": message})()))

    assert received == [b'{"hello":"world"}']


def test_issuer_for_activity_subject_is_gateway() -> None:
    assert issuer_for_subject("activity.events") == "gateway"
