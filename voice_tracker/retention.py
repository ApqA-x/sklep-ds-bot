"""Retention-инструмент архива: ТОЛЬКО отчёт (dry-run), удаления нет как функции.

T16 (п.6 плана): решение владельца (D04/D07) — удалённые Discord-сообщения и
вложения СОХРАНЯЮТСЯ, срок хранения не утверждён. Поэтому здесь намеренно
отсутствует ветка удаления: «janitor», тихо стирающего архив, быть не должно.
Когда срок будет назначен, механизм удаления — отдельная задача с отдельным
решением, а не флажок в этом отчёте.

Резервные копии (BACKUP_*, docs/runbook-backup.md) и пользовательский контент
(RETENTION_*, этот модуль) — разные параметры и разные решения, не смешивать.

Политика читается из окружения (или аргументов CLI, у него приоритет):
  RETENTION_DELETED_DAYS  — сколько дней лежать tombstone-записям; 0/пусто =
                            срок не назначен (кандидаты не считаются).

Запуск:
  python -m voice_tracker.retention report --uri mongodb://... --db voice_tracker [--media-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CHAT_COLLECTION = "chat_messages"


def deleted_days_from_env(env: Any = None) -> int:
    source = os.environ if env is None else env
    raw = str(source.get("RETENTION_DELETED_DAYS", "") or "").strip()
    if raw == "":
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def scan_deleted_messages(db: Any, *, days: int, now: datetime | None = None) -> dict[str, Any]:
    """Сколько tombstone-записей попали бы в выборку при назначенном сроке.

    Только count/снимок границ — операций удаления здесь нет и не будет:
    функция возвращает план, а исполнять план нечем.
    """
    if days <= 0:
        return {"configured": False, "days": 0, "candidates": None,
                "note": "RETENTION_DELETED_DAYS не задан — срок хранения не утверждён (D07)"}
    moment = now or datetime.now(timezone.utc)
    boundary = moment - timedelta(days=days)
    filter_ = {"deletedAt": {"$type": "date", "$lte": boundary}}
    candidates = int(db[CHAT_COLLECTION].count_documents(filter_))
    oldest = db[CHAT_COLLECTION].find(filter_, {"deletedAt": 1}).sort("deletedAt", 1).limit(1)
    oldest_doc = next(iter(oldest), None)
    return {
        "configured": True,
        "days": days,
        "boundary": boundary.isoformat(),
        "candidates": candidates,
        "oldestDeletedAt": oldest_doc.get("deletedAt").isoformat()
        if oldest_doc and isinstance(oldest_doc.get("deletedAt"), datetime)
        else None,
    }


def scan_orphan_media(db: Any, media_dir: str) -> dict[str, Any]:
    """Файлы на диске, на которые не ссылается ни одно сохранённое вложение.

    Ссылка tombstone-сообщения считается живой ссылкой: удалённое сообщение
    хранит вложения до решения D07. Сироты возможны только из сбоя записи —
    отчёт их показывает, удаление остаётся ручным решением оператора.
    """
    root = Path(media_dir)
    if not root.is_dir():
        return {"error": "media dir отсутствует", "files": 0, "bytes": 0}
    referenced: set[str] = set()
    cursor = db[CHAT_COLLECTION].find(
        {"attachments.stored": True}, {"attachments.path": 1, "_id": 0}
    )
    for doc in cursor:
        for att in doc.get("attachments") or []:
            if att.get("stored") and att.get("path"):
                referenced.add(str(att["path"]).replace("\\", "/"))
    files = 0
    total_bytes = 0
    samples: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel in referenced:
            continue
        files += 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        if len(samples) < 20:
            samples.append(rel)
    return {"files": files, "bytes": total_bytes, "samples": samples, "referenced": len(referenced)}


def report(uri: str, db_name: str, *, media_dir: str = "", days: int | None = None) -> dict[str, Any]:
    from pymongo import MongoClient

    configured_days = deleted_days_from_env() if days is None else max(0, int(days))
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        db = client[db_name]
        payload: dict[str, Any] = {
            "mode": "dry-run",
            "deletionImplemented": False,
            "decision": "D04/D07: удалённый контент сохраняется; срок не утверждён",
            "deletedMessages": scan_deleted_messages(db, days=configured_days),
        }
        if media_dir:
            payload["orphanMedia"] = scan_orphan_media(db, media_dir)
        return payload
    finally:
        client.close()


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="retention",
        description="Отчёт о состоянии архива. Удалений здесь нет и не будет до решения D07.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--uri", required=True)
    r.add_argument("--db", required=True)
    r.add_argument("--media-dir", default="")
    r.add_argument("--deleted-days", type=int, default=None,
                   help="переопределить RETENTION_DELETED_DAYS для отчёта")
    args = ap.parse_args(argv)
    payload = report(args.uri, args.db, media_dir=args.media_dir, days=args.deleted_days)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
