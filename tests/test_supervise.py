"""T12: supervisor, heartbeat и healthcheck CLI на ботовской стороне."""
from __future__ import annotations

import ast
import asyncio
import pathlib
import re
import sys
import types
from datetime import UTC, datetime, timedelta

import pytest

from voice_tracker import healthcheck as hc
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
    try:
        await asyncio.sleep(0.5)
        assert calls["n"] >= 3
        assert sup.unhealthy_tasks() == ["sweep"]
        snap = sup.snapshot()[0]
        assert snap["lastErrorType"] == "RuntimeError"
        assert snap["critical"] is True
    finally:
        await sup.shutdown()


async def test_loop_that_returns_is_treated_as_failure() -> None:
    sup = supervise.Supervisor(initial_backoff=0.01, backoff_cap=0.02, unhealthy_after=2)

    async def quits() -> None:
        return

    sup.spawn("quitter", quits, critical=True)
    try:
        await asyncio.sleep(0.3)
        assert sup.unhealthy_tasks() == ["quitter"]
    finally:
        await sup.shutdown()


async def test_beat_marks_progress_and_clears_failures() -> None:
    sup = supervise.Supervisor(initial_backoff=0.01, backoff_cap=0.02, unhealthy_after=2)

    async def flaky() -> None:
        sup.beat("flaky-loop")
        raise RuntimeError("boom")

    sup.spawn("flaky-loop", flaky, critical=True)
    try:
        await asyncio.sleep(0.3)
        # beat вызывается на каждом запуске до падения → серия не накапливается
        assert sup.unhealthy_tasks() == []
        assert sup._handles["flaky-loop"].restarts >= 2
    finally:
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

    def find_one(self, flt):
        return dict(self.docs[flt["worker"]]) if flt["worker"] in self.docs else None

    def update_one(self, flt, update, upsert=False):
        doc = self.docs.get(flt["worker"])
        matched = doc is not None and all(doc.get(k) == v for k, v in flt.items() if k != "$or")
        if matched:
            doc.update(update.get("$set", {}))
        elif upsert:
            new = {k: v for k, v in flt.items() if not str(k).startswith("$")}
            new.update(update.get("$setOnInsert", {}))
            new.update(update.get("$set", {}))
            self.docs[flt["worker"]] = new
        else:
            return
        self.writes.append(dict(self.docs[flt["worker"]]))

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
    assert doc["instance"] == supervise.INSTANCE_ID
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
    # нет is_ready на клиенте → getattr-safe: ready=None (не секрет и не «ready»)
    assert ds == {"closed": True, "ready": None, "latencyMs": None}

    class AliveDiscord:
        latency = 0.1234

        def is_closed(self) -> bool:
            return False

        def is_ready(self) -> bool:
            return True

    ds2 = supervise.discord_state(AliveDiscord())
    assert ds2 == {"closed": False, "ready": True, "latencyMs": 123.4}

    class ConnectingDiscord:
        """R26-10 r2: reconnecting-клиент ещё не closed, но и не ready."""

        latency = 0.2

        def is_closed(self) -> bool:
            return False

        def is_ready(self) -> bool:
            return False

    ds3 = supervise.discord_state(ConnectingDiscord())
    assert ds3 == {"closed": False, "ready": False, "latencyMs": 200.0}


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


# ------------------------------------------------------- E09 single-writer guard


def test_single_writer_evaluate_matrix() -> None:
    now = datetime.now(UTC)
    assert supervise.evaluate_single_writer(None, "host-a", now, 90.0) is None
    # наш же instance (рестарт контейнера) — не блокер даже со свежим beat
    mine = {"worker": "gateway", "instance": "host-a", "updated_at": now}
    assert supervise.evaluate_single_writer(mine, "host-a", now, 90.0) is None
    # чужой живой instance — отказ
    other = {"worker": "gateway", "instance": "host-b", "updated_at": now - timedelta(seconds=10)}
    reason = supervise.evaluate_single_writer(other, "host-a", now, 90.0)
    assert reason is not None and "host-b" in reason
    # чужой, но просроченный (аварийно умер) — пропускаем
    stale = {"worker": "gateway", "instance": "host-b", "updated_at": now - timedelta(seconds=200)}
    assert supervise.evaluate_single_writer(stale, "host-a", now, 90.0) is None
    # чужой, но снят graceful stop — пропускаем
    stopped = {"worker": "gateway", "instance": "host-b", "updated_at": now, "stopped": True}
    assert supervise.evaluate_single_writer(stopped, "host-a", now, 90.0) is None
    # BSON-наивный datetime
    naive = {"worker": "gateway", "instance": "host-b",
             "updated_at": (now - timedelta(seconds=5)).replace(tzinfo=None)}
    assert supervise.evaluate_single_writer(naive, "host-a", now, 90.0) is not None


def test_single_writer_claim_and_release() -> None:
    class _Coll:
        def __init__(self):
            self.docs = {}

        def find_one(self, flt):
            d = self.docs.get(flt["worker"])
            return dict(d) if d else None

        def update_one(self, flt, update, upsert=False):
            key = flt["worker"]
            if key in self.docs:
                self.docs[key].update(update["$set"])
            elif upsert:
                self.docs[key] = dict(update["$set"])

    coll = _Coll()

    class _Db:
        def __getitem__(self, name):
            assert name == supervise.HEARTBEAT_COLLECTION
            return coll

    real_db = _Db()
    supervise.claim_single_writer(real_db, "gateway", "host-a")
    assert coll.docs["gateway"]["instance"] == "host-a"
    # второй живой instance отвергается
    with pytest.raises(RuntimeError, match="host-a"):
        supervise.claim_single_writer(real_db, "gateway", "host-b")
    # рестарт того же instance — проходит
    supervise.claim_single_writer(real_db, "gateway", "host-a")
    # graceful stop снимает блок для нового instance
    supervise.release_single_writer(real_db, "gateway", "host-a")
    assert coll.docs["gateway"]["stopped"] is True
    supervise.claim_single_writer(real_db, "gateway", "host-b")
    assert coll.docs["gateway"]["instance"] == "host-b"


# ============================================================ R26-10 (V26-24)
# явные health contracts: fail-closed по loops/deps, startup grace, idle healthy


def _loop_entry(
    loop: hc.LoopContract,
    now: datetime,
    *,
    running: bool = True,
    failures: int = 0,
    tick: object = "auto",
) -> dict:
    if tick == "auto":
        tick = (
            now.isoformat(timespec="seconds")
            if loop.max_progress_age_seconds is not None
            else None
        )
    return {
        "name": loop.name,
        "critical": loop.critical,
        "running": running,
        "restarts": 0,
        "consecutiveFailures": failures,
        "lastErrorType": None,
        "lastErrorAt": None,
        "lastTickAt": tick,
        "startedAt": now.isoformat(timespec="seconds"),
    }


def _deps_for(contract: hc.HealthContract) -> dict:
    deps: dict = {}
    if "nats" in contract.deps:
        deps["nats"] = {"connected": True, "reconnecting": False, "closed": False}
    if "discord" in contract.deps:
        deps["discord"] = {"closed": False, "ready": True, "latencyMs": 40.0}
    return deps


def _healthy_doc(
    contract: hc.HealthContract,
    now: datetime,
    *,
    age: float = 5,
    started_age: float = 600,  # по умолчанию вне startup grace: проверяем steady state
) -> dict:
    return {
        "worker": contract.worker,
        "instance": "host-a",
        "started_at": now - timedelta(seconds=started_age),
        "updated_at": now - timedelta(seconds=age),
        "loops": [_loop_entry(l, now) for l in contract.loops],
        "deps": _deps_for(contract),
    }


def _set_loop(doc: dict, name: str, **changes: object) -> None:
    # алиасы коротких имён тестов → реальные ключи snapshot/evaluate (R26-10):
    # production-проверка читает consecutiveFailures/lastTickAt, не «failures/tick»
    if "tick" in changes:
        changes["lastTickAt"] = changes.pop("tick")
    if "failures" in changes:
        changes["consecutiveFailures"] = changes.pop("failures")
    for entry in doc["loops"]:
        if entry["name"] == name:
            entry.update(changes)
            return
    raise AssertionError(f"loop {name} not in doc")


CONTRACT_WORKERS = (
    "tracker",
    "writer",
    "gateway",
    "activity",
    "stalker",
    "commands",
    "dsbot-controlplane",
)

SPAWN_NAMES = {
    "tracker": {"tracker-event-sweep"},
    "writer": {"writer-event-sweep"},
    "gateway": {
        "gateway-event-sweep",
        "gateway-managed-voice-reconcile",
        "gateway-voice-session-reaper",
        "gateway-invite-snapshot-refresh",
        "gateway-invite-metadata-reconcile",
        "gateway-member-role-reconcile",
    },
    "activity": {"activity-event-sweep"},
    "stalker": {"stalker-event-sweep"},
    "commands": set(),
    "dsbot-controlplane": set(),
}


def test_v26_24_contracts_cover_seven_workers_with_service_spawn_names() -> None:
    assert len(CONTRACT_WORKERS) == 7
    for worker in CONTRACT_WORKERS:
        contract = hc.contract_for(worker)
        assert contract is not None and contract.worker == worker
        names = {loop.name for loop in contract.loops}
        assert f"{worker}-heartbeat" in names
        assert names - {f"{worker}-heartbeat"} == SPAWN_NAMES[worker]
    # controlplane: SERVICE-алиас ведёт к своему контракту
    assert hc.contract_for(worker_for("controlplane")) is not None
    # unknown identity нет контракта → CLI откажет (fail-closed)
    assert hc.contract_for("mystery") is None


def test_v26_24_fresh_heartbeat_with_dead_required_loop_fails() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("gateway")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    _set_loop(doc, "gateway-voice-session-reaper", running=False)
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "not running" in detail and "gateway-voice-session-reaper" in detail


def test_v26_24_required_loop_with_failures_fails() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("tracker")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    _set_loop(doc, "tracker-event-sweep", failures=2)
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "failing" in detail
    # non-critical петля может иметь ошибки, не роняя сервис
    gcontract = hc.contract_for("gateway")
    assert gcontract is not None
    gdoc = _healthy_doc(gcontract, now)
    _set_loop(gdoc, "gateway-invite-snapshot-refresh", failures=4)
    ok, _ = evaluate(gdoc, now, 90.0, gcontract)
    assert ok


def test_v26_24_stale_progress_fails_after_grace() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("writer")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    stale = (now - timedelta(seconds=300)).isoformat(timespec="seconds")
    _set_loop(doc, "writer-heartbeat", tick=stale)
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "progress stale" in detail and "writer-heartbeat" in detail
    # sweep с beat давно молчит → тоже stale
    doc2 = _healthy_doc(contract, now)
    _set_loop(doc2, "writer-event-sweep", tick=stale)
    ok2, detail2 = evaluate(doc2, now, 90.0, contract)
    assert not ok2 and "progress stale" in detail2


def test_v26_24_startup_grace_tolerates_missing_ticks_then_expires() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("activity")
    assert contract is not None
    doc = _healthy_doc(contract, now, started_age=10)
    for entry in doc["loops"]:
        entry["lastTickAt"] = None
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert ok and "startup-grace" in detail
    # тот же снапшот после истечения grace — fail-closed на missing tick
    late = _healthy_doc(contract, now, started_age=contract.startup_grace_seconds + 60)
    for entry in late["loops"]:
        entry["lastTickAt"] = None
    ok, detail = evaluate(late, now, 90.0, contract)
    assert not ok and "no progress tick" in detail
    # grace не прощает мёртвую петлю и оторванные зависимости
    dead = _healthy_doc(contract, now, started_age=1)
    _set_loop(dead, "activity-event-sweep", running=False)
    ok, _ = evaluate(dead, now, 90.0, contract)
    assert not ok


def test_v26_24_nats_disconnected_and_discord_closed_fail() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("stalker")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    doc["deps"]["nats"] = {"connected": False, "reconnecting": True, "closed": False}
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "nats" in detail
    doc = _healthy_doc(contract, now)
    doc["deps"]["nats"] = {"connected": None, "reconnecting": None, "closed": None}
    assert not evaluate(doc, now, 90.0, contract)[0]  # unknown state = invalid
    doc = _healthy_doc(contract, now)
    doc["deps"]["discord"] = {"closed": True, "latencyMs": None}
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "discord" in detail
    doc = _healthy_doc(contract, now)
    doc["deps"]["discord"] = {"closed": None, "latencyMs": None}
    assert not evaluate(doc, now, 90.0, contract)[0]


def test_v26_24_idle_without_user_events_is_healthy() -> None:
    # в доке нет никаких пользовательских событий — только петли/зависимости;
    # тишина R03: fresh heartbeat + healthy loops = OK
    now = datetime.now(UTC)
    for worker in CONTRACT_WORKERS:
        contract = hc.contract_for(worker)
        assert contract is not None
        ok, detail = evaluate(_healthy_doc(contract, now), now, 90.0, contract)
        assert ok, f"{worker}: {detail}"
        assert "fresh" in detail


def test_v26_24_unknown_or_missing_snapshot_fields_are_invalid() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("tracker")
    assert contract is not None
    base = _healthy_doc(contract, now)

    doc = dict(base)
    doc.pop("loops")
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "loops" in detail

    doc = _healthy_doc(contract, now)
    doc["loops"].append("garbage")
    assert not evaluate(doc, now, 90.0, contract)[0]

    doc = _healthy_doc(contract, now)
    doc["loops"] = [l for l in doc["loops"] if l["name"] != "tracker-event-sweep"]
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "missing" in detail

    doc = _healthy_doc(contract, now)
    _set_loop(doc, "tracker-event-sweep", running="yes")
    assert not evaluate(doc, now, 90.0, contract)[0]
    _set_loop(doc, "tracker-event-sweep", running=True, consecutiveFailures="0")
    assert not evaluate(doc, now, 90.0, contract)[0]
    _set_loop(doc, "tracker-heartbeat", tick="not-a-date")
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "lastTickAt" in detail

    doc = _healthy_doc(contract, now)
    doc.pop("deps")
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "deps" in detail
    doc = _healthy_doc(contract, now)
    doc["deps"]["nats"] = "up"
    assert not evaluate(doc, now, 90.0, contract)[0]

    # старый док без started_at: grace считается от updated_at, но живые тики ОК
    doc = _healthy_doc(contract, now)
    doc.pop("started_at")
    assert evaluate(doc, now, 90.0, contract)[0]


def test_v26_24_stale_heartbeat_short_circuits_contract() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("gateway")
    assert contract is not None
    doc = _healthy_doc(contract, now, age=600)
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "stale" in detail


# ============================================================ R26-10 (V26-25)
# Supervisor: итерации успех/неуспех внутри while; heartbeat life ≠ progress;
# CLI identity/timeouts/safe errors.


async def test_v25_fail_and_beat_separate_loop_life_from_progress() -> None:
    sup = supervise.Supervisor()

    async def forever() -> None:
        await asyncio.sleep(3600)

    sup.spawn("writer-heartbeat", forever, critical=True)
    try:
        await asyncio.sleep(0.05)
        snap = sup.snapshot()[0]
        assert snap["running"] is True and snap["startedAt"] is not None  # жизнь
        assert snap["lastTickAt"] is None and snap["consecutiveFailures"] == 0

        sup.fail("writer-heartbeat", RuntimeError("auth mongodb://user:secret@host"))
        snap = sup.snapshot()[0]
        assert snap["running"] is True  # цикл жив, respawn не триггерится
        assert snap["restarts"] == 0
        assert snap["consecutiveFailures"] == 1
        assert snap["lastErrorType"] == "RuntimeError"
        assert "mongodb" not in str(snap["lastErrorType"])
        assert snap["lastTickAt"] is None  # успешного прогресса так и не было

        sup.fail("writer-heartbeat", "write failed mongodb://user:pass@host/db")
        snap = sup.snapshot()[0]
        assert snap["consecutiveFailures"] == 2
        label = str(snap["lastErrorType"])
        # метка остаётся «плоской»: без пробелов, URL-разделителей и обрезана по длине
        assert re.fullmatch(r"[0-9A-Za-z_.-]+", label)
        assert "://" not in label and " " not in label
        assert len(label) <= 64

        # восстановление: следующая успешная итерация сбрасывает серию, ошибка видна
        sup.beat("writer-heartbeat")
        snap = sup.snapshot()[0]
        assert snap["consecutiveFailures"] == 0
        assert snap["lastTickAt"] is not None
        assert snap["lastErrorAt"] is not None
        assert snap["running"] is True
    finally:
        await sup.shutdown()
    # unknown task name — no-op, не падение
    sup.fail("no-such-loop", "boom")


async def test_v25_recovery_clears_unhealthy_tasks() -> None:
    sup = supervise.Supervisor(unhealthy_after=2)

    async def forever() -> None:
        await asyncio.sleep(3600)

    sup.spawn("sweep", forever, critical=True)
    try:
        await asyncio.sleep(0.05)
        sup.fail("sweep", "MongoUnavailable")
        sup.fail("sweep", "MongoUnavailable")
        assert sup.unhealthy_tasks() == ["sweep"]
        contract = hc.contract_for("tracker")
        assert contract is not None
        doc = _healthy_doc(contract, datetime.now(UTC))
        _set_loop(doc, "tracker-event-sweep", failures=2)
        assert not evaluate(doc, datetime.now(UTC), 90.0, contract)[0]
        sup.beat("sweep")
        assert sup.unhealthy_tasks() == []
        doc_ok = _healthy_doc(contract, datetime.now(UTC))
        assert evaluate(doc_ok, datetime.now(UTC), 90.0, contract)[0]
    finally:
        await sup.shutdown()


async def test_v25_heartbeat_doc_marks_started_at_and_iteration_failure() -> None:
    db = RecordingDB()
    sup = supervise.Supervisor()
    hb = supervise.Heartbeat(db, "writer", sup, interval_seconds=0.02)
    supervise.attach(sup, hb)
    try:
        await asyncio.sleep(0.08)
        doc = db.coll.docs["writer"]
        assert doc["started_at"] == supervise.PROCESS_STARTED_AT
        entry = next(l for l in doc["loops"] if l["name"] == "writer-heartbeat")
        assert entry["running"] is True and entry["lastTickAt"] is not None
        assert entry["consecutiveFailures"] == 0

        db.coll.fail_times = 5  # записи падают: цикл жив, прогресса нет
        await asyncio.sleep(0.08)
        snap = next(l for l in sup.snapshot() if l["name"] == "writer-heartbeat")
        assert snap["running"] is True
        assert snap["consecutiveFailures"] >= 1
        assert snap["lastErrorType"] == "RuntimeError"
        assert "secret" not in str(snap["lastErrorType"])
        assert snap["lastTickAt"] is not None  # прошлый успех ≠ текущая жизнь
    finally:
        await sup.shutdown()


def _fake_pymongo(monkeypatch: pytest.MonkeyPatch, doc: dict | None, error: Exception | None = None):
    calls: dict = {}

    class _Collection:
        def find_one(self, flt):
            calls["filter"] = flt
            if error is not None:
                raise error
            return doc

    class _DB:
        def __getitem__(self, name: str):
            calls["collection"] = name
            return _Collection()

    class _Client:
        def __init__(self, uri: str, **kwargs):
            calls["uri"] = uri
            calls["kwargs"] = kwargs

        def __getitem__(self, name: str):
            return _DB()

        def close(self) -> None:
            calls["closed"] = True

    module = types.ModuleType("pymongo")
    module.MongoClient = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymongo", module)
    return calls


def _mongo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGO_URI", "mongodb://user:secret@host:27017")
    monkeypatch.setenv("MONGO_DB", "botdb")
    monkeypatch.delenv("SERVICE_NAME", raising=False)
    monkeypatch.delenv("SERVICE", raising=False)


def test_v25_cli_service_identity_precedence_and_alias(monkeypatch, capsys) -> None:
    _mongo_env(monkeypatch)
    now = datetime.now(UTC)
    # SERVICE_NAME > SERVICE
    monkeypatch.setenv("SERVICE", "tracker")
    monkeypatch.setenv("SERVICE_NAME", "gateway")
    calls = _fake_pymongo(monkeypatch, _healthy_doc(hc.contract_for("gateway"), now))
    assert healthcheck_main([]) == 0
    assert calls["filter"] == {"worker": "gateway"}
    assert calls["collection"] == "bot_runtime_heartbeats"
    # явный --service бьёт окружение
    calls = _fake_pymongo(monkeypatch, _healthy_doc(hc.contract_for("writer"), now))
    assert healthcheck_main(["--service", "writer"]) == 0
    assert calls["filter"] == {"worker": "writer"}
    # controlplane → dsbot-controlplane
    calls = _fake_pymongo(
        monkeypatch, _healthy_doc(hc.contract_for("dsbot-controlplane"), now)
    )
    assert healthcheck_main(["--service", "controlplane"]) == 0
    assert calls["filter"] == {"worker": "dsbot-controlplane"}
    # ничто не задано → tracker
    monkeypatch.delenv("SERVICE")
    monkeypatch.delenv("SERVICE_NAME")
    calls = _fake_pymongo(monkeypatch, _healthy_doc(hc.contract_for("tracker"), now))
    assert healthcheck_main([]) == 0
    assert calls["filter"] == {"worker": "tracker"}
    assert "secret" not in capsys.readouterr().out


def test_v25_cli_mongo_timeouts_are_bounded(monkeypatch) -> None:
    _mongo_env(monkeypatch)
    calls = _fake_pymongo(monkeypatch, _healthy_doc(hc.contract_for("tracker"), datetime.now(UTC)))
    assert healthcheck_main([]) == 0
    kwargs = calls["kwargs"]
    assert 0 < kwargs["serverSelectionTimeoutMS"] <= 3000
    assert 0 < kwargs["connectTimeoutMS"] <= 10000
    assert 0 < kwargs["socketTimeoutMS"] <= 10000


def test_v25_cli_unhealthy_exit_code_and_safe_errors(monkeypatch, capsys) -> None:
    _mongo_env(monkeypatch)
    now = datetime.now(UTC)
    contract = hc.contract_for("gateway")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    _set_loop(doc, "gateway-event-sweep", running=False)
    _fake_pymongo(monkeypatch, doc)
    assert healthcheck_main(["--service", "gateway"]) == 1
    out = capsys.readouterr().out
    assert "gateway-event-sweep" in out and "not running" in out
    assert "mongodb://" not in out

    _fake_pymongo(
        monkeypatch,
        None,
        error=RuntimeError("auth failed mongodb://user:secretpass@host/db"),
    )
    assert healthcheck_main([]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "mongodb://" not in err and "secretpass" not in err


def test_v25_cli_unknown_service_fails_closed_without_db(monkeypatch, capsys) -> None:
    _mongo_env(monkeypatch)
    calls = _fake_pymongo(monkeypatch, None)
    assert healthcheck_main(["--service", "mystery"]) == 1
    err = capsys.readouterr().err
    assert "contract" in err and "mystery" in err
    assert "filter" not in calls  # до Mongo не доходило: неизвестная identity = invalid


# ============================================================ R26-10.3
# production loops: проглоченное исключение наблюдаемо (fail), успех — beat;
# контракты: progress-age по реальным интервалам, non-critical не гейтит readiness.

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# имена spawn-ов, найденные в services/*.py (сверено с контрактами выше)
SERVICE_LOOP_FILES = {
    "tracker": ["tracker-event-sweep"],
    "writer": ["writer-event-sweep"],
    "activity": ["activity-event-sweep"],
    "stalker": ["stalker-event-sweep"],
    "gateway": [
        "gateway-event-sweep",
        "gateway-managed-voice-reconcile",
        "gateway-voice-session-reaper",
        "gateway-invite-snapshot-refresh",
        "gateway-invite-metadata-reconcile",
        "gateway-member-role-reconcile",
    ],
}


def _service_loop_signal_calls(service: str) -> tuple[set[str], set[str], set[str]]:
    """(spawned, beaten, failed) — имена loops, для которых в исходниках сервиса
    есть supervisor.spawn/beat/fail с этим name-аргументом (AST, не grep)."""
    tree = ast.parse((REPO_ROOT / "services" / f"{service}.py").read_text(encoding="utf-8"))
    spawned: set[str] = set()
    beaten: set[str] = set()
    failed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "supervisor":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            loop_name = node.args[0].value
        else:
            continue
        if node.func.attr == "spawn":
            spawned.add(loop_name)
        elif node.func.attr == "beat":
            beaten.add(loop_name)
        elif node.func.attr == "fail":
            failed.add(loop_name)
    return spawned, beaten, failed


@pytest.mark.parametrize("service", sorted(SERVICE_LOOP_FILES))
def test_v26_10_3_every_supervised_loop_is_instrumented_fail_and_beat(service: str) -> None:
    spawned, beaten, failed = _service_loop_signal_calls(service)
    assert spawned == set(SERVICE_LOOP_FILES[service])
    # каждый spawn-нутый цикл отмечает и неуспех итерации, и успех: иначе
    # consecutiveFailures/lastTickAt в heartbeat никогда не появятся
    assert beaten >= spawned, f"{service}: loops without beat(): {spawned - beaten}"
    assert failed >= spawned, f"{service}: loops without fail(): {spawned - failed}"


def test_v26_10_3_dockerfile_sets_service_and_healthcheck_relies_on_precedence() -> None:
    # Точка 6: Dockerfile запекает только ENV SERVICE (без SERVICE_NAME);
    # healthcheck без --service обязан резолвить identity по SERVICE.
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV SERVICE=${SERVICE}" in dockerfile
    assert "SERVICE_NAME" not in dockerfile
    assert "python -m voice_tracker.healthcheck" in dockerfile
    # precedence: SERVICE_NAME бьёт SERVICE, SERVICE работает без SERVICE_NAME
    assert hc.resolve_service(None, {"SERVICE_NAME": "gateway", "SERVICE": "tracker"}) == "gateway"
    assert hc.resolve_service(None, {"SERVICE": "writer"}) == "writer"
    assert hc.resolve_service(None, {}) == "tracker"


async def test_v26_10_3_swallowed_exception_raises_failures_then_success_resets() -> None:
    """Ровно паттерн production-loop: исключение внутри while проглатывается,
    отмечается fail(); полностью успешная итерация — beat()."""
    sup = supervise.Supervisor()
    ok_iterations = {"n": 0}
    fail_next = {"remaining": 2}

    async def sweep_like() -> None:
        # бесконечный while с try/except, как в services/*.py
        while True:
            await asyncio.sleep(0.01)
            try:
                if fail_next["remaining"] > 0:
                    fail_next["remaining"] -= 1
                    raise RuntimeError("mongo writePreference unavailable")
                ok_iterations["n"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sup.fail("writer-event-sweep", exc)
                continue
            sup.beat("writer-event-sweep")

    sup.spawn("writer-event-sweep", sweep_like, critical=True)
    try:
        await asyncio.sleep(0.2)
        snap = sup._handles["writer-event-sweep"].snapshot()
        assert snap["running"] is True  # цикл не умирал: respawn не при чём
        assert snap["restarts"] == 0
        # две проглоченные ошибки подняли серию ровно на 2 (не дублируется)
        # и не «съели» успех последующих итераций:
        assert ok_iterations["n"] > 0
        assert snap["consecutiveFailures"] == 0  # beat после успеха сбросил
        assert snap["lastTickAt"] is not None  # и обновил отметку прогресса
        assert snap["lastErrorType"] == "RuntimeError"
        assert snap["lastErrorAt"] is not None
    finally:
        await sup.shutdown()


async def test_v26_10_3_persistent_failures_unhealthy_then_recovery_healthy() -> None:
    """fail() на каждой итерации → unhealthy_tasks; beat() → снова ровно 0."""
    sup = supervise.Supervisor(unhealthy_after=3)

    async def forever() -> None:
        await asyncio.sleep(3600)

    sup.spawn("tracker-event-sweep", forever, critical=True)
    try:
        assert sup.unhealthy_tasks() == []
        for _ in range(3):
            sup.fail("tracker-event-sweep", RuntimeError("nats down"))
        assert sup.unhealthy_tasks() == ["tracker-event-sweep"]
        contract = hc.contract_for("tracker")
        assert contract is not None
        now = datetime.now(UTC)
        doc = _healthy_doc(contract, now)
        _set_loop(doc, "tracker-event-sweep", failures=3, lastErrorType="RuntimeError")
        ok, detail = evaluate(doc, now, 90.0, contract)
        assert not ok and "failing" in detail
        # восстановление наблюдаемо: успешная итерация вернула счётчик в 0
        sup.beat("tracker-event-sweep")
        assert sup.unhealthy_tasks() == []
        doc_ok = _healthy_doc(contract, now)
        assert evaluate(doc_ok, now, 90.0, contract)[0]
    finally:
        await sup.shutdown()


def test_v26_10_3_every_critical_loop_has_progress_age_contract() -> None:
    """Все mandatory critical loops застрахованы от «живой, но залипший» —
    у каждого задан max_progress_age_seconds; non-critical — явно безгейтны."""
    for worker in CONTRACT_WORKERS:
        contract = hc.contract_for(worker)
        assert contract is not None
        for loop in contract.loops:
            if loop.critical:
                assert loop.max_progress_age_seconds is not None, (worker, loop.name)
                assert loop.max_progress_age_seconds > 0
            else:
                assert loop.max_progress_age_seconds is None, (worker, loop.name)
    # heartbeat: 15s интервал × запас; sweep 15..60s: 180s; reconcile 5s: 90s;
    # reaper первый тик 90+120s: 300s
    assert hc.HEARTBEAT_LOOP_PROGRESS_AGE_SECONDS > 3 * 15
    assert hc.EVENT_SWEEP_PROGRESS_AGE_SECONDS >= 3 * 60
    assert hc.VOICE_RECONCILE_PROGRESS_AGE_SECONDS > 5 * 3
    assert hc.VOICE_REAPER_PROGRESS_AGE_SECONDS > 90 + 120


def test_v26_10_3_long_intervals_are_not_stale_before_their_time() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("gateway")
    assert contract is not None
    ages = {l.name: l.max_progress_age_seconds for l in contract.loops}
    doc = _healthy_doc(contract, now)
    # каждый critical loop на границе «чуть старше своего интервала, но внутри
    # договора» — не stale (запас есть, false positive запрещён)
    _set_loop(doc, "gateway-event-sweep", tick=(now - timedelta(seconds=100)).isoformat(timespec="seconds"))
    _set_loop(doc, "gateway-managed-voice-reconcile", tick=(now - timedelta(seconds=45)).isoformat(timespec="seconds"))
    _set_loop(doc, "gateway-voice-session-reaper", tick=(now - timedelta(seconds=250)).isoformat(timespec="seconds"))
    _set_loop(doc, "gateway-heartbeat", tick=(now - timedelta(seconds=40)).isoformat(timespec="seconds"))
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert ok, detail
    # и каждый из них просрочен за пределами договора
    for name in (
        "gateway-event-sweep",
        "gateway-managed-voice-reconcile",
        "gateway-voice-session-reaper",
        "gateway-heartbeat",
    ):
        overdue = _healthy_doc(contract, now)
        limit = ages[name]
        _set_loop(
            overdue, name, tick=(now - timedelta(seconds=limit + 30)).isoformat(timespec="seconds")
        )
        ok, detail = evaluate(overdue, now, 90.0, contract)
        assert not ok and "progress stale" in detail and name in detail, (name, detail)


def test_v26_10_3_noncritical_loops_never_gate_readiness() -> None:
    """Non-critical invite/member loops: падают, молчат или отсутствуют как
    «running» — readiness не снимается (иначе это были бы critical)."""
    now = datetime.now(UTC)
    contract = hc.contract_for("gateway")
    assert contract is not None
    noncritical = [l.name for l in contract.loops if not l.critical]
    assert set(noncritical) == {
        "gateway-invite-snapshot-refresh",
        "gateway-invite-metadata-reconcile",
        "gateway-member-role-reconcile",
    }
    for name in noncritical:
        for changes in (
            {"running": False},
            {"failures": 9},
            {"tick": None},
            {"tick": (now - timedelta(days=1)).isoformat(timespec="seconds")},
        ):
            doc = _healthy_doc(contract, now)
            _set_loop(doc, name, **changes)
            ok, detail = evaluate(doc, now, 90.0, contract)
            assert ok, (name, changes, detail)


# ============================================================ R26-10 review r2
# ДЕФЕКТ 1: closed=False ≠ ready (discord gateway).
# ДЕФЕКТ 2: будущие timestamps обходят freshness/progress-гейты и startup grace.


class _NotReadyDiscord:
    """Сессия в reconnect/waiting: ещё не closed, но гейт не готов."""

    latency = 0.05

    def is_closed(self) -> bool:
        return False

    def is_ready(self) -> bool:
        return False


def test_v26_10_r2_discord_not_ready_fails_closed_despite_open_gateway() -> None:
    # (1) discord_state публикует явный ready=False
    state = supervise.discord_state(_NotReadyDiscord())
    assert state["closed"] is False and state["ready"] is False
    # (2) fresh heartbeat + deps из такого состояния → not-ready
    now = datetime.now(UTC)
    for worker in ("gateway", "activity", "stalker", "commands", "dsbot-controlplane"):
        contract = hc.contract_for(worker)
        assert contract is not None and "discord" in contract.deps
        doc = _healthy_doc(contract, now)
        doc["deps"]["discord"] = state
        ok, detail = evaluate(doc, now, 90.0, contract)
        assert not ok, f"{worker}: {detail}"
        assert "discord" in detail and "ready" in detail
    # ready=True при closed=False — по-прежнему healthy (позитив не сломан)
    contract = hc.contract_for("gateway")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    assert evaluate(doc, now, 90.0, contract)[0]


def test_v26_10_r2_discord_snapshot_without_ready_is_unknown_and_invalid() -> None:
    # старый/битый снапшот без поля ready → fail-closed (не «deps ok» молча)
    now = datetime.now(UTC)
    contract = hc.contract_for("stalker")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    doc["deps"]["discord"] = {"closed": False, "latencyMs": 40.0}  # ready missing
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "ready state unknown" in detail
    doc = _healthy_doc(contract, now)
    doc["deps"]["discord"] = {"closed": False, "ready": None, "latencyMs": None}
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "ready state unknown" in detail


def test_v26_10_r2_future_updated_at_fails_closed() -> None:
    now = datetime.now(UTC)
    future = now + timedelta(days=1)
    # возраст «-86400s» не может означать «свежо» — проверка возраста только
    # для не-future timestamp'ов
    ok, detail = evaluate({"updated_at": future}, now, 90.0)
    assert not ok and "timestamp in future" in detail and "updated_at" in detail
    contract = hc.contract_for("tracker")
    assert contract is not None
    doc = _healthy_doc(contract, future)  # весь док «из завтрашнего дня»
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "timestamp in future" in detail


def test_v26_10_r2_future_started_at_does_not_extend_grace_forever() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("activity")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    doc["started_at"] = now + timedelta(days=1)  # grace = (now - started) = -86400s
    for entry in doc["loops"]:
        entry["lastTickAt"] = None  # и без прогресса — grace не должен спасать
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "timestamp in future" in detail and "started_at" in detail


def test_v26_10_r2_future_lastTickAt_fails_closed() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("tracker")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    _set_loop(
        doc,
        "tracker-event-sweep",
        tick=(now + timedelta(days=1)).isoformat(timespec="seconds"),
    )
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "timestamp in future" in detail
    assert "lastTickAt" in detail and "tracker-event-sweep" in detail
    # и во время startup grace future-тик тоже не принимается
    grace_doc = _healthy_doc(contract, now, started_age=10)
    _set_loop(
        grace_doc,
        "tracker-event-sweep",
        tick=(now + timedelta(days=1)).isoformat(timespec="seconds"),
    )
    ok, detail = evaluate(grace_doc, now, 90.0, contract)
    assert not ok and "timestamp in future" in detail
    # и для non-critical петли с max_progress… (гейта нет, но clock всё равно
    # недостоверен): снапшот с future-тиком не проходит
    gcontract = hc.contract_for("gateway")
    assert gcontract is not None
    gdoc = _healthy_doc(gcontract, now)
    _set_loop(
        gdoc,
        "gateway-invite-snapshot-refresh",
        tick=(now + timedelta(days=1)).isoformat(timespec="seconds"),
    )
    ok, detail = evaluate(gdoc, now, 90.0, gcontract)
    assert not ok and "timestamp in future" in detail


def test_v26_10_r2_small_clock_skew_stays_ready() -> None:
    # NTP-порядок (|skew| <= MAX_CLOCK_SKEW_SECONDS) не роняет readiness:
    # слегка «из будущего» updated_at/lastTickAt/started_at — норма
    now = datetime.now(UTC)
    contract = hc.contract_for("gateway")
    assert contract is not None
    skew = timedelta(seconds=10)
    assert skew.total_seconds() <= hc.MAX_CLOCK_SKEW_SECONDS
    doc = _healthy_doc(contract, now, started_age=600)
    doc["updated_at"] = now + skew
    doc["started_at"] = now - timedelta(seconds=600) + skew
    for entry in doc["loops"]:
        if entry["lastTickAt"] is not None:
            entry["lastTickAt"] = (now + skew).isoformat(timespec="seconds")
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert ok, detail
    assert "fresh" in detail


# ============================================================ R26-10 review r3
# ДЕФЕКТ 1: NATS с closed=None/missing (unknown-состояние) не должен проходить
# readiness — supervise.nats_state() честно отдаёт None, когда getattr бросил.
# ДЕФЕКТ 2: непарсящийся lastTickAt/started_at не должен молча превращаться в
# «ещё не было тика» / вечно активную startup grace.


def test_v26_10_r3_nats_missing_closed_is_not_healthy() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("tracker")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    doc["deps"]["nats"] = {"connected": True, "reconnecting": False}  # closed missing
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "nats" in detail


def test_v26_10_r3_nats_none_closed_is_not_healthy() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("tracker")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    doc["deps"]["nats"] = {"connected": True, "reconnecting": False, "closed": None}
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "nats" in detail


def test_v26_10_r3_nats_reconnecting_or_unknown_not_healthy() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("writer")
    assert contract is not None
    for state in (
        {"connected": True, "reconnecting": True, "closed": False},  # переподключение
        {"connected": True, "reconnecting": None, "closed": False},  # unknown
        {"connected": True, "reconnecting": False, "closed": "up"},  # не-bool
        {"connected": True, "reconnecting": False},  # missing
    ):
        doc = _healthy_doc(contract, now)
        doc["deps"]["nats"] = state
        ok, detail = evaluate(doc, now, 90.0, contract)
        assert not ok, (state, detail)
        assert "nats" in detail, (state, detail)


def test_v26_10_r3_nats_fully_valid_stays_healthy() -> None:
    # перекос запрещён: все три явных bool на месте → readiness не снимается
    now = datetime.now(UTC)
    checked = 0
    for worker in CONTRACT_WORKERS:
        contract = hc.contract_for(worker)
        assert contract is not None
        if "nats" not in contract.deps:
            continue
        checked += 1
        doc = _healthy_doc(contract, now)
        doc["deps"]["nats"] = {"connected": True, "reconnecting": False, "closed": False}
        ok, detail = evaluate(doc, now, 90.0, contract)
        assert ok, (worker, detail)
    assert checked == 6


def test_v26_10_r3_malformed_lasttick_in_grace_rejected() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("tracker")
    assert contract is not None
    doc = _healthy_doc(contract, now, started_age=5)  # внутри startup grace
    _set_loop(doc, "tracker-event-sweep", tick="not-a-date")
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "lastTickAt" in detail


def test_v26_10_r3_malformed_lasttick_noncritical_rejected() -> None:
    # битое поле = битый снапшот: отказ и для non-critical петли (её readiness
    # не гейтится по прогрессу, но валидность снапшота едина для всех)
    now = datetime.now(UTC)
    contract = hc.contract_for("gateway")
    assert contract is not None
    doc = _healthy_doc(contract, now)
    _set_loop(doc, "gateway-invite-snapshot-refresh", tick="not-a-date")
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "lastTickAt" in detail


def test_v26_10_r3_malformed_started_at_rejected() -> None:
    now = datetime.now(UTC)
    contract = hc.contract_for("tracker")
    assert contract is not None
    doc = _healthy_doc(contract, now, started_age=5)
    doc["started_at"] = "not-a-date"
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert not ok and "started_at" in detail


def test_v26_10_r3_absent_started_at_still_compatible() -> None:
    # док вообще без started_at (старый формат): grace считается от updated_at
    now = datetime.now(UTC)
    contract = hc.contract_for("tracker")
    assert contract is not None
    doc = _healthy_doc(contract, now, age=5, started_age=600)
    doc.pop("started_at")
    ok, detail = evaluate(doc, now, 90.0, contract)
    assert ok, detail
