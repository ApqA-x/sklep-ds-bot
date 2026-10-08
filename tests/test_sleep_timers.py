from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Lock
from types import SimpleNamespace

import pytest

from voice_tracker.sleep_timers import SleepTimerBusy, SleepTimerStore


NOW = datetime(2026, 10, 8, 21, 0, tzinfo=UTC)


class DuplicateKeyError(Exception):
    code = 11000


class Collection:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.lock = Lock()
        self.fail_first_cas = False

    def find_one(self, flt: dict) -> dict | None:
        with self.lock:
            return deepcopy(self.docs.get(flt["_id"]))

    def insert_one(self, doc: dict) -> None:
        with self.lock:
            if doc["_id"] in self.docs:
                raise DuplicateKeyError()
            self.docs[doc["_id"]] = deepcopy(doc)

    def replace_one(self, flt: dict, doc: dict):
        with self.lock:
            if self.fail_first_cas:
                self.fail_first_cas = False
                return SimpleNamespace(matched_count=0)
            old = self.docs.get(flt["_id"])
            if old is None or old["revision"] != flt["revision"] or old["status"] != flt["status"]:
                return SimpleNamespace(matched_count=0)
            self.docs[flt["_id"]] = deepcopy(doc)
            return SimpleNamespace(matched_count=1)


def set_timer(store: SleepTimerStore, hours: int, request_id: str, now: datetime = NOW):
    return store.set(
        "456", "123", hours, actor_user_id="789",
        source="slash", request_id=request_id, now=now,
    )


def cancel_timer(store: SleepTimerStore, request_id: str, now: datetime = NOW):
    return store.cancel(
        "456", "123", actor_user_id="789",
        source="slash", request_id=request_id, now=now,
    )


def test_set_is_wall_clock_and_replay_cannot_extend_deadline() -> None:
    store = SleepTimerStore(Collection())
    first = set_timer(store, 2, "interaction-1")
    assert first.timer["dueAt"] == datetime(2026, 10, 8, 23, 0, tzinfo=UTC)
    assert first.timer["revision"] == 1
    replay = set_timer(store, 2, "interaction-1", NOW + timedelta(minutes=30))
    assert replay.replayed is True
    assert replay.outcome == first.outcome
    assert replay.timer["revision"] == 1
    assert replay.timer["dueAt"] == first.timer["dueAt"]


def test_replace_and_cancel_are_atomic_and_idempotent() -> None:
    collection = Collection()
    store = SleepTimerStore(collection)
    set_timer(store, 2, "set-1")
    replaced = set_timer(store, 3, "set-2", NOW + timedelta(minutes=5))
    assert replaced.timer["revision"] == 2
    assert replaced.timer["dueAt"] == NOW + timedelta(hours=3, minutes=5)
    cancelled = cancel_timer(store, "cancel-1", NOW + timedelta(minutes=6))
    assert cancelled.timer["status"] == "cancelled"
    assert cancelled.timer["revision"] == 3
    assert cancel_timer(store, "cancel-1", NOW + timedelta(hours=1)).replayed
    assert collection.docs["456:123"]["revision"] == 3
    # An older replay cannot revive a timer after a newer cancellation.
    assert set_timer(store, 2, "set-1", NOW + timedelta(hours=1)).replayed
    assert store.get("456", "123")["status"] == "cancelled"


def test_concurrent_sets_serialize_without_lost_request_ids() -> None:
    store = SleepTimerStore(Collection())
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(set_timer, store, 2, "a")
        b = pool.submit(set_timer, store, 3, "b")
        assert {a.result().outcome["requestId"], b.result().outcome["requestId"]} == {"a", "b"}
    doc = store.get("456", "123")
    assert doc["revision"] == 2
    assert {item["requestId"] for item in doc["recentRequests"]} == {"a", "b"}


def test_bounded_cas_retry_and_executing_conflict() -> None:
    collection = Collection()
    store = SleepTimerStore(collection)
    set_timer(store, 2, "set-1")
    collection.fail_first_cas = True
    assert set_timer(store, 4, "set-2").timer["revision"] == 2
    collection.docs["456:123"]["status"] = "executing"
    with pytest.raises(SleepTimerBusy):
        cancel_timer(store, "cancel-1")
    assert store.get("456", "123")["revision"] == 2


@pytest.mark.parametrize("hours", [0, 25, 1.5, True, "2"])
def test_invalid_hours_rejected(hours) -> None:
    with pytest.raises(ValueError, match="hours"):
        set_timer(SleepTimerStore(Collection()), hours, "request")


def test_reused_request_id_with_different_parameters_is_rejected() -> None:
    store = SleepTimerStore(Collection())
    set_timer(store, 2, "same")
    with pytest.raises(ValueError, match="request_id reused"):
        set_timer(store, 3, "same")
    with pytest.raises(ValueError, match="request_id reused"):
        store.set(
            "456", "123", 2, actor_user_id="987",
            source="slash", request_id="same", now=NOW,
        )


def test_target_must_be_a_discord_id_not_a_free_text_nickname() -> None:
    store = SleepTimerStore(Collection())
    with pytest.raises(ValueError, match="snowflake"):
        store.set(
            "456", "same nickname", 2, actor_user_id="789",
            source="web", request_id="request-1", now=NOW,
        )
