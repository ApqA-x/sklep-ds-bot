"""T12/R26-10: контейнерный healthcheck сервиса бота.

Проверяет не «любой ответ процесса», а признак работоспособности: свежий
heartbeat этого воркера в Mongo + явный health contract воркера (R26-10):
required loop-ы живы (running), не в серии отказов (consecutiveFailures == 0),
у required loops с max_progress_age последний успешный тик не старше договора,
required зависимости (NATS/Discord) в правильном состоянии. Тишина
пользовательских событий на него не влияет — heartbeat пишется по таймеру.

Fail-closed: unknown/missing/битые поля контракта или снапшота — unhealthy.
В startup grace (по started_at heartbeat-дока) допускается ещё не набитый
lastTickAt — цикл стартовал, но период тика еще не истёк.

Выход с ненулевым кодом помечает контейнер unhealthy. Сам по себе unhealthy
контейнер не рестартует (см. docs/runbook-health.md): рестарт — ответственность
restart policy, а health/status — сигнал readiness для оператора и монитора.

Вывод — только service/worker, возраст heartbeat, имена loops/deps и имена
ошибок (без URI/секретов). Identity: --service > SERVICE_NAME > SERVICE >
tracker; controlplane маппится на worker dsbot-controlplane.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

DEFAULT_MAX_AGE_SECONDS = 90.0
SERVER_SELECTION_TIMEOUT_MS = 1500
CONNECT_TIMEOUT_MS = 3000
SOCKET_TIMEOUT_MS = 5000
DEFAULT_STARTUP_GRACE_SECONDS = 120.0
# R26-10 r2: снапшот с timestamp в будущем (boot сбитых часов воркера) не должен
# обходить freshness/progress-гейты; допуск — порядок NTP-дрейфа
MAX_CLOCK_SKEW_SECONDS = 30.0
# heartbeat-цикл пишет каждые ~15s; три пропущенных тика — уже не «жив»
HEARTBEAT_LOOP_PROGRESS_AGE_SECONDS = 60.0
# sweep-циклы: tracker/activity/stalker спят EVENT_SWEEP_INTERVAL_SECONDS (15s),
# writer — 60s, gateway event sweep — 60s; 180s = минимум 3× худшего интервала
EVENT_SWEEP_PROGRESS_AGE_SECONDS = 180.0
# managed-voice reconcile: интервал 5s; 90s = 18× с запасом на длинные
# Discord-вызовы внутри одной итерации
VOICE_RECONCILE_PROGRESS_AGE_SECONDS = 90.0
# voice-session reaper: первый тик через 90s после старта, далее каждые 120s;
# 300s покрывает и «холодный» первый интервал (90+120=210s), и 2,5× штатного
VOICE_REAPER_PROGRESS_AGE_SECONDS = 300.0

# worker-имена исторически отличаются от SERVICE у controlplane
_WORKER_ALIASES = {"controlplane": "dsbot-controlplane"}

_LABEL_SAFE = re.compile(r"[^0-9A-Za-z_.-]")


def worker_for(service: str) -> str:
    return _WORKER_ALIASES.get(service, service)


@dataclass(frozen=True)
class LoopContract:
    """Один цикл воркера в health-контракте.

    critical=True: петля обязана быть running и без серии отказов; если задан
    max_progress_age_seconds — последний успешный beat() не старше договора
    (для non-critical прогресс-гейт не применяется: они наблюдаемы в снапшоте,
    но readiness не снимают).

    critical=False: проверяется только presence и валидность полей снапшота —
    падающий non-critical цикл виден в heartbeat/логах, но сервис остаётся
    ready (рестарт и починка — ответственность supervisor-respawn и оператора).
    max_progress_age_seconds для таких петель сознательно не задаётся."""

    name: str
    critical: bool = True
    max_progress_age_seconds: float | None = None


@dataclass(frozen=True)
class HealthContract:
    worker: str
    loops: tuple[LoopContract, ...]
    deps: tuple[str, ...] = ()
    startup_grace_seconds: float = DEFAULT_STARTUP_GRACE_SECONDS


def _heartbeat_loop(worker: str) -> LoopContract:
    return LoopContract(f"{worker}-heartbeat", True, HEARTBEAT_LOOP_PROGRESS_AGE_SECONDS)


# Имена петель сверены с supervisor.spawn(...) в services/*.py (R26-10).
_CONTRACTS: dict[str, HealthContract] = {
    "tracker": HealthContract(
        "tracker",
        (
            _heartbeat_loop("tracker"),
            LoopContract("tracker-event-sweep", True, EVENT_SWEEP_PROGRESS_AGE_SECONDS),
        ),
        ("nats",),
    ),
    "writer": HealthContract(
        "writer",
        (
            _heartbeat_loop("writer"),
            LoopContract("writer-event-sweep", True, EVENT_SWEEP_PROGRESS_AGE_SECONDS),
        ),
        ("nats",),
    ),
    "gateway": HealthContract(
        "gateway",
        (
            _heartbeat_loop("gateway"),
            LoopContract("gateway-event-sweep", True, EVENT_SWEEP_PROGRESS_AGE_SECONDS),
            LoopContract(
                "gateway-managed-voice-reconcile", True, VOICE_RECONCILE_PROGRESS_AGE_SECONDS
            ),
            LoopContract("gateway-voice-session-reaper", True, VOICE_REAPER_PROGRESS_AGE_SECONDS),
            # non-critical: presence/validity в снапшоте, readiness не снимают
            LoopContract("gateway-invite-snapshot-refresh", False),
            LoopContract("gateway-invite-metadata-reconcile", False),
            LoopContract("gateway-member-role-reconcile", False),
        ),
        ("nats", "discord"),
    ),
    "activity": HealthContract(
        "activity",
        (
            _heartbeat_loop("activity"),
            LoopContract("activity-event-sweep", True, EVENT_SWEEP_PROGRESS_AGE_SECONDS),
        ),
        ("nats", "discord"),
    ),
    "stalker": HealthContract(
        "stalker",
        (
            _heartbeat_loop("stalker"),
            LoopContract("stalker-event-sweep", True, EVENT_SWEEP_PROGRESS_AGE_SECONDS),
        ),
        ("nats", "discord"),
    ),
    "commands": HealthContract(
        "commands",
        (_heartbeat_loop("commands"),),
        ("discord",),
    ),
    "dsbot-controlplane": HealthContract(
        "dsbot-controlplane",
        (_heartbeat_loop("dsbot-controlplane"),),
        ("nats", "discord"),
    ),
}


def contract_for(worker: str) -> HealthContract | None:
    return _CONTRACTS.get(worker)


def resolve_service(explicit: str | None, environ: Mapping[str, str]) -> str:
    """R26-10: явный --service > SERVICE_NAME > SERVICE > tracker."""
    for candidate in (explicit, environ.get("SERVICE_NAME"), environ.get("SERVICE")):
        text = str(candidate or "").strip()
        if text:
            return text
    return "tracker"


def _as_utc(value: Any) -> datetime | None:
    """BSON datetime приезжает наивным UTC; supervisor пишет ISO-строки."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_label(text: str, limit: int = 64) -> str:
    cleaned = _LABEL_SAFE.sub("", text)
    return cleaned[:limit] or "unknown"


def _future_timestamp_error(parsed: datetime, now: datetime, field: str) -> str | None:
    """R26-10 r2: timestamp позже now + допустимого skew — снапшот недостоверен.

    Fail-closed: future updated_at делает heartbeat «вечно свежим», future
    started_at растягивает startup grace на неопределённый срок, future
    lastTickAt обходит progress-гейт. Малый future-skew (|skew| <= лимит) —
    допустимый дрейф часов, не ошибка."""
    skew = (parsed - now).total_seconds()
    if skew > MAX_CLOCK_SKEW_SECONDS:
        return (
            f"timestamp in future field={_safe_label(field)} "
            f"skew={int(skew)}s limit={int(MAX_CLOCK_SKEW_SECONDS)}s"
        )
    return None


def _evaluate_loops(
    doc: dict[str, Any], now: datetime, contract: HealthContract, in_grace: bool
) -> tuple[bool, str]:
    loops_raw = doc.get("loops")
    if not isinstance(loops_raw, list):
        return False, "heartbeat has no loops snapshot"
    by_name: dict[str, dict[str, Any]] = {}
    for entry in loops_raw:
        if not isinstance(entry, dict):
            return False, "heartbeat has invalid loop entry"
        name = entry.get("name")
        if isinstance(name, str) and name:
            by_name[name] = entry
    for loop in contract.loops:
        entry = by_name.get(loop.name)
        if entry is None:
            return False, f"required loop missing name={_safe_label(loop.name)}"
        running = entry.get("running")
        failures = entry.get("consecutiveFailures")
        if not isinstance(running, bool):
            return False, f"loop invalid running name={_safe_label(loop.name)}"
        if isinstance(failures, bool) or not isinstance(failures, int):
            return False, f"loop invalid consecutiveFailures name={_safe_label(loop.name)}"
        # R26-10 r2: future lastTickAt (сверх skew) никогда не станет stale —
        # отсекаем до progress-гейта и независимо от startup grace
        raw_tick = entry.get("lastTickAt")
        if raw_tick is not None:
            parsed_tick = _as_utc(raw_tick)
            if parsed_tick is not None:
                future = _future_timestamp_error(parsed_tick, now, f"lastTickAt.{loop.name}")
                if future is not None:
                    return False, future
        if loop.critical:
            if not running:
                return False, f"required loop not running name={_safe_label(loop.name)}"
            if failures != 0:
                return False, (
                    f"required loop failing name={_safe_label(loop.name)} "
                    f"consecutiveFailures={failures}"
                )
        if loop.max_progress_age_seconds is not None and loop.critical and not in_grace:
            tick = _as_utc(entry.get("lastTickAt"))
            if entry.get("lastTickAt") is None:
                return False, f"required loop has no progress tick name={_safe_label(loop.name)}"
            if tick is None:
                return False, f"loop invalid lastTickAt name={_safe_label(loop.name)}"
            tick_age = (now - tick).total_seconds()
            if tick_age > loop.max_progress_age_seconds:
                return False, (
                    f"loop progress stale name={_safe_label(loop.name)} "
                    f"age={int(tick_age)}s limit={int(loop.max_progress_age_seconds)}s"
                )
    return True, ""


def _evaluate_deps(doc: dict[str, Any], contract: HealthContract) -> tuple[bool, str]:
    deps = doc.get("deps")
    if not isinstance(deps, dict):
        return False, "heartbeat has no deps snapshot"
    for name in contract.deps:
        state = deps.get(name)
        if not isinstance(state, dict):
            return False, f"required dep missing name={_safe_label(name)}"
        if name == "nats":
            if state.get("connected") is not True or state.get("closed") is True:
                return False, "nats not connected"
        elif name == "discord":
            if state.get("closed") is not False:
                return False, "discord gateway not open"
            # R26-10.1: «не closed» ≠ «ready». Failed session, который ещё не
            # закрыт, остаётся closed=False — гейт открывает только is_ready().
            # missing/None = старый или битый снапшот без ready-сигнала → fail-closed.
            ready = state.get("ready")
            if ready is None:
                return False, "discord gateway ready state unknown"
            if ready is not True:
                return False, "discord gateway not ready"
        else:
            return False, f"unknown dep in contract name={_safe_label(name)}"
    return True, ""


def evaluate(
    doc: Any, now: datetime, max_age_seconds: float, contract: HealthContract | None = None
) -> tuple[bool, str]:
    """Чистая функция проверки — тестируется без Mongo.

    contract=None — только возраст heartbeat (совместимость); с contract —
    fail-closed по loops/deps воркера."""
    if doc is None:
        return False, "no heartbeat yet"
    if not isinstance(doc, dict):
        return False, "heartbeat document is invalid"
    updated = _as_utc(doc.get("updated_at"))
    if updated is None:
        return False, "heartbeat missing updated_at"
    future = _future_timestamp_error(updated, now, "updated_at")
    if future is not None:
        return False, future
    age = (now - updated).total_seconds()
    if age > max_age_seconds:
        return False, f"heartbeat stale age={int(age)}s limit={int(max_age_seconds)}s"
    if contract is None:
        return True, f"heartbeat fresh age={int(age)}s"
    started = _as_utc(doc.get("started_at"))
    if started is not None:
        future = _future_timestamp_error(started, now, "started_at")
        if future is not None:
            return False, future
    else:
        started = updated
    in_grace = (now - started).total_seconds() <= contract.startup_grace_seconds
    ok, detail = _evaluate_loops(doc, now, contract, in_grace)
    if not ok:
        return False, detail
    ok, detail = _evaluate_deps(doc, contract)
    if not ok:
        return False, detail
    suffix = " startup-grace" if in_grace else ""
    return True, f"heartbeat fresh age={int(age)}s loops={len(contract.loops)} deps ok{suffix}"


def main(argv: list[str] | None = None) -> int:
    from os import environ

    parser = argparse.ArgumentParser(prog="voice_tracker.healthcheck")
    parser.add_argument(
        "--service",
        default=None,
        help="identity сервиса; без флага: SERVICE_NAME > SERVICE > tracker",
    )
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)

    service = resolve_service(args.service, environ)
    worker = worker_for(service)
    contract = contract_for(worker)
    if contract is None:
        print(
            f"healthcheck: no health contract for service={_safe_label(service)} "
            f"worker={_safe_label(worker)}",
            file=sys.stderr,
        )
        return 1

    uri = (environ.get("MONGO_URI") or "").strip()
    db_name = (environ.get("MONGO_DB") or "").strip()
    if not uri or not db_name:
        print("healthcheck: MONGO_URI/MONGO_DB not configured", file=sys.stderr)
        return 1

    try:
        from pymongo import MongoClient

        client = MongoClient(
            uri,
            serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
            connectTimeoutMS=CONNECT_TIMEOUT_MS,
            socketTimeoutMS=SOCKET_TIMEOUT_MS,
        )
        try:
            doc = client[db_name]["bot_runtime_heartbeats"].find_one({"worker": worker})
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — healthcheck не должен печатать окружение
        print(f"healthcheck: worker={worker} db error {type(exc).__name__}", file=sys.stderr)
        return 1

    ok, detail = evaluate(doc, datetime.now(UTC), args.max_age, contract)
    print(f"healthcheck: worker={worker} {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
