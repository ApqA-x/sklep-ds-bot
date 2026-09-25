"""T12: supervisor, heartbeat и healthcheck CLI на ботовской стороне."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from voice_tracker import supervise
from voice_tracker.healthcheck import evaluate, main as healthcheck_main, worker_for


# ----------------------------------------------------------------- backoff


def test_backoff_bounds_and_cap() -> None:
    for attempt in range(1, 10):
        for _ in range(20):
            value = supervise.backoff_seconds(attempt, initial=1.0, cap=8.0)
            base = min(8.0, 2.0 ** (attempt - 1))
            assert base / 2 <= value <= base


def test_backoff_jitter_varies() -> None:
    assert len({supervise.backoff_seconds(3) for _ in range(30)}) > 1


def test_writer_startup_backoff_has_jitter() -> None:
    from services import writer

    values = {writer._startup_backoff_seconds(2) for _ in range(20)}
    assert len(values) > 1
    assert all(1.0 <= v <= 2.0 for v in values)  # base=2, полный джиттер в [1, 2]
    assert max(writer._startup_backoff_seconds(a) for a in range(1, 30)) <= 30.0


# ---------------------------------------------------------------- supervisor


async def test_failing_loop_respawns_and_marks_unhealthy() -> None:
    sup = supervise.Supervisor(initial_backoff=0.01, backoff_cap=0.02, unhealthy_after=3)
    calls = {"n": 0}

    async def dying() -> None:
        calls["n"] += 1
        raise RuntimeError("boom")

    sup.spawn("sweep", dying, critical=True)
    await asyncio.sleep(0.5)
    assert calls["n"] >= 3
    assert sup.unhealthy_tasks() == ["sweep"]
    snap = sup.snapshot()[0]
    assert snap["lastErrorType"] == "RuntimeError"
    assert snap["critical"] is True
    await sup.shutdown()


async def test_loop_that_returns_is_treated_as_failure() -> None:
    sup = supervise.Supervisor(initial_backoff=0.01, backoff_cap=0.02, unhealthy_after=2)

    async def quits() -> None:
        return

    sup.spawn("quitter", quits, critical=True)
    await asyncio.sleep(0.3)
    assert sup.unhealthy_tasks() == ["quitter"]
    await sup.shutdown()


async def test_beat_marks_progress_and_clears_failures() -> None:
    sup = supervise.Supervisor(initial_backoff=0.01, backoff_cap=0.02, unhealthy_after=2)

    async def flaky() -> None:
        sup.beat("flaky-loop")
        raise RuntimeError("boom")

    sup.spawn("flaky-loop", flaky, critical=True)
    await asyncio.sleep(0.3)
    # beat вызывается на каждом запуске до падения → серия не накапливается
    assert sup.unhealthy_tasks() == []
    assert sup._handles["flaky-loop"].restarts >= 2
    await sup.shutdown()


async def test_shutdown_cancels_and_awaits_drain() -> None:
    sup = supervise.Supervisor()
    state = {"drained": False}

    async def worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            state["drained"] = True

    sup.spawn("worker", worker)
    await asyncio.sleep(0.05)
    await sup.shutdown(timeout=2.0)
    assert state["drained"] is True
    with pytest.raises(RuntimeError):
        sup.spawn("late", worker)


# ----------------------------------------------------------------- heartbeat


class RecordingCollection:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.writes: list[dict] = []
        self.fail_times = 0

    def replace_one(self, flt, doc, upsert=False) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("mongo down mongodb://secret@host")
        self.writes.append(dict(doc))
        self.docs[flt["worker"]] = dict(doc)


class RecordingDB:
    def __init__(self) -> None:
        self.coll = RecordingCollection()

    def __getitem__(self, name: str) -> RecordingCollection:
        assert name == "bot_runtime_heartbeats"
        return self.coll


async def test_heartbeat_writes_loops_and_deps() -> None:
    db = RecordingDB()
    sup = supervise.Supervisor()
    hb = supervise.Heartbeat(
        db,
        "writer",
        sup,
        state_fn=lambda: {"nats": {"connected": True}},
        interval_seconds=0.02,
    )
    supervise.attach(sup, hb)
    await asyncio.sleep(0.15)
    await sup.shutdown()
    doc = db.coll.docs["writer"]
    assert isinstance(doc["updated_at"], datetime)
    assert doc["deps"] == {"nats": {"connected": True}}
    names = {loop["name"] for loop in doc["loops"]}
    assert "writer-heartbeat" in names
    assert doc["heartbeatErrors"] == 0


async def test_heartbeat_survives_transient_db_errors() -> None:
    db = RecordingDB()
    db.coll.fail_times = 3  # первые три записи падают
    sup = supervise.Supervisor()
    hb = supervise.Heartbeat(db, "tracker", sup, interval_seconds=0.02)
    supervise.attach(sup, hb)
    await asyncio.sleep(0.2)
    await sup.shutdown()
    # цикл не умер: после трёх упавших тиков запись появилась, и в доке видно
    # пережитые сбои БД (счётчик на момент первой успешной записи = 3)
    assert db.coll.writes, "heartbeat loop died on transient DB errors"
    assert db.coll.writes[0]["heartbeatErrors"] == 3


def test_nats_and_discord_state_are_getattr_safe() -> None:
    class Conn:
        is_connected = True
        is_reconnecting = False

        @property
        def is_closed(self):  # attention: property to exercise attribute errors
            raise RuntimeError("weird client")

    st = supervise.nats_state(Conn())
    assert st == {"connected": True, "reconnecting": False, "closed": None}

    class DeadDiscord:
        def is_closed(self) -> bool:
            return True

        @property
        def latency(self):
            raise RuntimeError("latency before connection")

    ds = supervise.discord_state(DeadDiscord())
    assert ds == {"closed": True, "latencyMs": None}

    class AliveDiscord:
        latency = 0.1234

        def is_closed(self) -> bool:
            return False

    ds2 = supervise.discord_state(AliveDiscord())
    assert ds2 == {"closed": False, "latencyMs": 123.4}


# ---------------------------------------------------------------- healthcheck


def test_worker_for_keeps_controlplane_alias() -> None:
    assert worker_for("controlplane") == "dsbot-controlplane"
    assert worker_for("writer") == "writer"


def test_evaluate_fresh_stale_missing() -> None:
    now = datetime.now(UTC)
    ok, detail = evaluate({"updated_at": now - timedelta(seconds=5)}, now, 90.0)
    assert ok and "fresh" in detail

    ok, detail = evaluate({"updated_at": now - timedelta(seconds=200)}, now, 90.0)
    assert not ok and "stale" in detail

    # BSON datetime приезжает наивным UTC
    naive = (now - timedelta(seconds=10)).replace(tzinfo=None)
    ok, _ = evaluate({"updated_at": naive}, now, 90.0)
    assert ok

    ok, detail = evaluate(None, now, 90.0)
    assert not ok and "no heartbeat" in detail

    ok, _ = evaluate({}, now, 90.0)
    assert not ok

    ok, _ = evaluate({"updated_at": "not-a-date"}, now, 90.0)
    assert not ok


def test_healthcheck_main_requires_env(monkeypatch, capsys) -> None:
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.delenv("MONGO_DB", raising=False)
    code = healthcheck_main(["--service", "writer"])
    assert code == 1
    err = capsys.readouterr().err
    assert "MONGO_URI" in err
    assert "mongodb://" not in err
