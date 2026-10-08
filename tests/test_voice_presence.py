from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from voice_tracker.voice_presence import VoicePresenceTracker


class Collection:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.fail_write = False

    def replace_one(self, flt: dict, doc: dict, *, upsert: bool) -> None:
        if self.fail_write:
            raise RuntimeError("database unavailable")
        self.docs[flt["_id"]] = dict(doc)

    def find_one(self, flt: dict) -> dict | None:
        return self.docs.get(flt["_id"])


class PausedCollection(Collection):
    def __init__(self) -> None:
        super().__init__()
        self.pause = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def replace_one(self, flt: dict, doc: dict, *, upsert: bool) -> None:
        if self.pause:
            self.entered.set()
            assert self.release.wait(2)
        super().replace_one(flt, doc, upsert=upsert)


def state(channel_id: str | None):
    return SimpleNamespace(channel=SimpleNamespace(id=channel_id) if channel_id else None)


def member():
    return SimpleNamespace(id=123, guild=SimpleNamespace(id=456))


def guild(channel_id: str | None):
    states = {123: state(channel_id)} if channel_id else {}
    return SimpleNamespace(id=456, voice_states=states, unavailable=False)


async def proof(tracker: VoicePresenceTracker, due_at: datetime, channel_id: str = "1") -> bool:
    return await tracker.proves_presence("456", "123", due_at, channel_id, gateway_ready=True)


@pytest.mark.asyncio
async def test_channel_move_keeps_continuous_stay() -> None:
    collection = Collection()
    tracker = VoicePresenceTracker(collection)
    await tracker.seed([guild("1")])
    await asyncio.sleep(0.03)  # require a distinct clock tick after the seed
    due_at = datetime.now(UTC)
    await tracker.observe(member(), state("1"), state("2"))
    assert await proof(tracker, due_at, "2")
    assert not await proof(tracker, due_at, "1")


@pytest.mark.asyncio
async def test_leave_and_rejoin_after_deadline_never_reuses_old_stay() -> None:
    tracker = VoicePresenceTracker(Collection())
    await tracker.seed([guild("1")])
    due_at = datetime.now(UTC)
    await tracker.observe(member(), state("1"), state(None))
    await tracker.observe(member(), state(None), state("1"))
    assert not await proof(tracker, due_at)
    await asyncio.sleep(0.03)  # Windows wall clock may have a 15.6 ms tick.
    assert await proof(tracker, datetime.now(UTC))


@pytest.mark.asyncio
async def test_absent_at_deadline_then_join_is_not_proof() -> None:
    tracker = VoicePresenceTracker(Collection())
    await tracker.seed([guild(None)])
    due_at = datetime.now(UTC)
    await tracker.observe(member(), state(None), state("1"))
    assert not await proof(tracker, due_at)


@pytest.mark.asyncio
async def test_reconnect_cannot_attest_past_deadline() -> None:
    tracker = VoicePresenceTracker(Collection())
    await tracker.seed([guild("1")])
    due_at = datetime.now(UTC)
    tracker.disconnected()
    assert not await proof(tracker, due_at)
    await tracker.seed([guild("1")])
    assert not await proof(tracker, due_at)


@pytest.mark.asyncio
async def test_failed_leave_write_invalidates_old_positive_evidence() -> None:
    collection = Collection()
    tracker = VoicePresenceTracker(collection)
    await tracker.seed([guild("1")])
    due_at = datetime.now(UTC)
    collection.fail_write = True
    with pytest.raises(RuntimeError, match="database unavailable"):
        await tracker.observe(member(), state("1"), state(None))
    collection.fail_write = False
    assert not await proof(tracker, due_at)


@pytest.mark.asyncio
async def test_stale_persisted_state_or_lost_lease_cannot_prove_presence() -> None:
    collection = Collection()
    tracker = VoicePresenceTracker(collection)
    await tracker.seed([guild("1")])
    due_at = datetime.now(UTC)
    collection.docs["456:123"]["gatewayGeneration"] = "old-owner"
    assert not await proof(tracker, due_at)
    collection.docs["456:123"]["gatewayGeneration"] = tracker._generation

    def lost_lease() -> None:
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        await tracker.proves_presence(
            "456", "123", due_at, "1", gateway_ready=True, lease_check=lost_lease
        )
    assert not await tracker.proves_presence(
        "456", "123", due_at + timedelta(hours=1), "1", gateway_ready=True
    )


@pytest.mark.asyncio
async def test_queued_leave_blocks_proof_before_mongo_write_finishes() -> None:
    collection = PausedCollection()
    tracker = VoicePresenceTracker(collection)
    await tracker.seed([guild("1")])
    due_at = datetime.now(UTC)
    collection.pause = True
    leaving = asyncio.create_task(tracker.observe(member(), state("1"), state(None)))
    await asyncio.to_thread(collection.entered.wait, 2)
    try:
        assert not await proof(tracker, due_at)
    finally:
        collection.release.set()
        await leaving


@pytest.mark.asyncio
async def test_disconnect_during_seed_does_not_reenable_evidence() -> None:
    collection = PausedCollection()
    collection.pause = True
    tracker = VoicePresenceTracker(collection)
    seeding = asyncio.create_task(tracker.seed([guild("1")]))
    await asyncio.to_thread(collection.entered.wait, 2)
    tracker.disconnected()
    collection.release.set()
    await seeding
    assert not await proof(tracker, datetime.now(UTC))
