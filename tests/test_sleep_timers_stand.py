"""Sleep timer CAS and replay on the isolated Mongo integration stand."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pymongo import MongoClient

from stand_guard import guard_db_name, guard_mongo_uri
from voice_tracker.sleep_timers import SleepTimerStore


pytestmark = pytest.mark.integration
TEST_MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")


def test_concurrent_set_and_replay_on_real_mongo() -> None:
    guard_mongo_uri(TEST_MONGO_URI)
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=2500)
    try:
        try:
            client.admin.command("ping")
        except Exception:
            pytest.skip("isolated Mongo stand is unavailable")
        db_name = f"voice_tracker_tsleep_{uuid.uuid4().hex[:10]}"
        guard_db_name(db_name)
        db = client[db_name]
        try:
            store = SleepTimerStore(db["voice_sleep_timers"])
            now = datetime(2026, 10, 8, 21, tzinfo=UTC)

            def set_one(request_id: str, hours: int):
                return store.set(
                    "456", "123", hours, actor_user_id="789", source="slash",
                    request_id=request_id, now=now,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(set_one, "request-a", 2)
                b = pool.submit(set_one, "request-b", 3)
                assert a.result().timer["revision"] in {1, 2}
                assert b.result().timer["revision"] in {1, 2}
            saved = store.get("456", "123")
            assert saved["revision"] == 2
            assert {item["requestId"] for item in saved["recentRequests"]} == {"request-a", "request-b"}
            assert set_one("request-a", 2).replayed is True
            assert store.get("456", "123")["revision"] == 2
        finally:
            client.drop_database(db_name)
    finally:
        client.close()
