"""T12: контейнерный healthcheck сервиса бота.

Проверяет не «любой ответ процесса», а признак работоспособности: свежий
heartbeat этого воркера в Mongo (loop жив + запись в БД проходит). Тишина
пользовательских событий на него не влияет — heartbeat пишется по таймеру.

Выход с ненулевым кодом помечает контейнер unhealthy. Сам по себе unhealthy
контейнер не рестартует (см. docs/runbook-health.md): рестарт — ответственность
restart policy, а health/status — сигнал readiness для оператора и монитора.

Вывод — только имя воркера, возраст heartbeat и имя ошибки (без URI/секретов).
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

DEFAULT_MAX_AGE_SECONDS = 90.0
SERVER_SELECTION_TIMEOUT_MS = 1500

# worker-имена исторически отличаются от SERVICE у controlplane
_WORKER_ALIASES = {"controlplane": "dsbot-controlplane"}


def worker_for(service: str) -> str:
    return _WORKER_ALIASES.get(service, service)


def evaluate(doc: Any, now: datetime, max_age_seconds: float) -> tuple[bool, str]:
    """Чистая функция проверки — тестируется без Mongo."""
    if doc is None:
        return False, "no heartbeat yet"
    updated = doc.get("updated_at")
    if not isinstance(updated, datetime):
        return False, "heartbeat missing updated_at"
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    age = (now - updated).total_seconds()
    if age > max_age_seconds:
        return False, f"heartbeat stale age={int(age)}s limit={int(max_age_seconds)}s"
    return True, f"heartbeat fresh age={int(age)}s"


def main(argv: list[str] | None = None) -> int:
    from os import environ

    parser = argparse.ArgumentParser(prog="voice_tracker.healthcheck")
    parser.add_argument("--service", default=environ.get("SERVICE", "tracker"))
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)

    uri = (environ.get("MONGO_URI") or "").strip()
    db_name = (environ.get("MONGO_DB") or "").strip()
    if not uri or not db_name:
        print("healthcheck: MONGO_URI/MONGO_DB not configured", file=sys.stderr)
        return 1

    worker = worker_for(args.service.strip() or "tracker")
    try:
        from pymongo import MongoClient

        client = MongoClient(uri, serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS)
        try:
            doc = client[db_name]["bot_runtime_heartbeats"].find_one({"worker": worker})
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — healthcheck не должен печатать окружение
        print(f"healthcheck: worker={worker} db error {type(exc).__name__}", file=sys.stderr)
        return 1

    ok, detail = evaluate(doc, datetime.now(UTC), args.max_age)
    print(f"healthcheck: worker={worker} {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
