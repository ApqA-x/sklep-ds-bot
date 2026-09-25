"""T10: versioned migration runner для voice_tracker (единственный управляемый
шаг DDL). Приложения на startup только проверяют совместимость; удаление и
пересоздание индексов — здесь.

Команды:
    python -m voice_tracker.migrate status              [--db NAME]
    python -m voice_tracker.migrate plan  [--only N]    # dry-run: что сделает
    python -m voice_tracker.migrate up    [--only N] [--dry-run]
    python -m voice_tracker.migrate snapshot --out FILE  # read-only, без документов
    python -m voice_tracker.migrate export-manifest --out FILE
    python -m voice_tracker.migrate users               # least-privilege (DB06)

Идемпотентность: повтор `up` на уже применённой версии — no-op (create_index с
той же спецификацией noop; статус-документ не трогается). Смена манифеста без
новых миграций (checksum) фиксируется в schema_versions, DDL не переигрывается.
Прерывание (DB04): конкурентный runner отбивается lease-локом schema_lock;
сбой посередине оставляет статус running — повторный `up` после истечения
лока продолжает ту же миграцию (все шаги идемпотентны; уникальные индексы
строятся только после чистого precheck дублей).
Восстановление после прерывания: `status` показывает running/failed с
startedAt; `up` безопасен к повтору; принудительный сброс — только вручную
после разбора (инструкция в docs/adr/0003).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from . import schema
from .schema import COLLECTIONS, MANIFEST, SCHEMA_VERSION

LOCK_COLL = "schema_lock"
MIG_COLL = "schema_migrations"
VERSION_COLL = "schema_versions"
LOCK_LEASE_SECONDS = 120


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ миграции

@dataclass(frozen=True)
class Migration:
    id: int
    name: str
    apply: Callable[[Any, bool], dict]  # (db, dry_run) -> report
    backward_compatible: bool = True  # DB07: откат приложения поверх этой версии безопасен


def _apply_baseline(db: Any, dry: bool) -> dict:
    """M1: контракт индексов (бот+веб+шаренные). Эквивалентная спецификация под
    другим именем принимается как есть (DB01) — не удалять и не дублировать."""
    created: list[str] = []
    accepted_alias: list[str] = []
    cache: dict[str, list[dict]] = {}
    for spec in MANIFEST:
        if spec.owner not in ("bot", "web", "shared"):
            continue  # additive-индексы — в своих миграциях; legacy — только документированы
        if dry:
            created.append(f"{spec.collection}.{spec.name}")
            continue
        actual = cache.get(spec.collection)
        if actual is None:
            actual = schema._list_indexes(db, spec.collection)
            if actual is None:  # фейк без list_indexes — alias считаем отсутствующим
                actual = []
            cache[spec.collection] = actual
        alias = [doc for doc in actual
                 if schema.actual_keys(doc) == spec.keys and not schema._flags_match(doc, spec)]
        if alias and alias[0].get("name") != spec.name:
            accepted_alias.append(f"{spec.collection}.{spec.name}~({alias[0].get('name')})")
            continue
        db[spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
        created.append(f"{spec.collection}.{spec.name}")
    return {"indexes": created, "acceptedAlias": accepted_alias, "dryRun": dry}


VOLATILE_FIELDS = ("_id", "updatedAt", "lastSeen", "capturedAt")


def _dup_groups(db: Any, coll: str, keys: list[str]) -> dict:
    """Read-only поиск групп-дублей по ключам (до 500 групп). Документы группы
    считаются доказуемо эквивалентными только при совпадении всего payload без
    изменчивых полей; иначе группа «конфликтная» — удаление запрещено (DB05)."""
    pipeline = [
        {"$match": {k: {"$exists": True} for k in keys}},
        {"$group": {"_id": {k: f"${k}" for k in keys}, "ids": {"$push": "$_id"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
        {"$limit": 500},
    ]
    groups = []
    for g in db[coll].aggregate(pipeline):
        key = dict(g["_id"])  # Mongo возвращает _id группы как документ {guildId: ..., entryId: ...}
        docs = list(db[coll].find(key, {"_id": 1}))
        sigs: set[str] = set()
        for d in docs:
            full = db[coll].find_one({"_id": d["_id"]}) or {}
            sigs.add(json.dumps({k: v for k, v in sorted(full.items(), key=lambda kv: str(kv[0]))
                                 if k not in VOLATILE_FIELDS}, sort_keys=True, default=str))
        groups.append({"key": key, "ids": list(g["ids"]), "n": g["n"],
                       "identical": len(sigs) == 1})
    return {"collection": coll, "keys": keys, "duplicateGroups": len(groups),
            "mergeable": sum(1 for g in groups if g["identical"]),
            "conflicting": sum(1 for g in groups if not g["identical"]),
            "groups": groups}


def _apply_discord_audit_unique(db: Any, dry: bool) -> dict:
    """M3: unique (guildId, entryId) поверх отчёта о дублях (DB05).
    Молча НЕ удаляет неодинаковые документы: конфликт → abort с репортом."""
    dups = _dup_groups(db, schema.DA, ["guildId", "entryId"])
    if dups["conflicting"]:
        raise RuntimeError(
            f"discord_audit_logs: {dups['conflicting']} групп с НЕодинаковыми документами по "
            f"(guildId, entryId) — unique-индекс не строится, слепое удаление запрещено. "
            f"Отчёт: {json.dumps(dups['groups'][:20], ensure_ascii=False, default=str)}"
        )
    if dry:
        return {"duplicates": dups, "dryRun": True}
    merged = 0
    for g in dups["groups"]:
        if not g["identical"]:
            continue
        keep = min(g["ids"], key=str)
        delete = [i for i in g["ids"] if i != keep]
        if delete:
            res = db[schema.DA].delete_many(
                {"_id": {"$in": delete}, "guildId": g["key"]["guildId"], "entryId": g["key"]["entryId"]})
            merged += res.deleted_count
    spec = next(s for s in MANIFEST if s.name == "discord_audit_guildId_entryId_unique")
    db[spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
    return {"duplicates": dups, "mergedDeleted": merged, "uniqueIndex": spec.name,
            "note": "web_disc_audit_guildId_entryId (не-unique) остаётся до ручного drop после подтверждения"}


def _apply_operations_ttl(db: Any, dry: bool) -> dict:
    spec = next(s for s in MANIFEST if s.name == "operations_createdAt_ttl")
    if not dry:
        db[spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
    return {"index": f"{spec.collection}.{spec.name}", "ttlSeconds": spec.ttl, "dryRun": dry}


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline-index-contract", _apply_baseline, backward_compatible=True),
    Migration(2, "operations-journal-ttl", _apply_operations_ttl, backward_compatible=True),
    Migration(3, "discord-audit-entry-unique", _apply_discord_audit_unique, backward_compatible=False),
)


def migration_checksum(mig: Migration) -> str:
    # checksum контракта, из которого вырос шаг: смена манифеста меняет его → детект дрейфа
    return schema.manifest_checksum()[:16]


# ------------------------------------------------------------------ лок/статус


def _expired_filter(now: datetime) -> dict:
    return {"_id": "migrate",
            "$or": [{"leaseExpiresAt": {"$exists": False}}, {"leaseExpiresAt": {"$lte": now}}]}


def acquire_lock(db: Any, owner: str) -> dict | None:
    """Атомарный lease-лок: перехват просроченного (или первого) через CAS по _id.
    Активный лок другого runner'а → None (DB04: параллельные миграции не допускаются)."""
    now = _utc_now()
    expiry = now + timedelta(seconds=LOCK_LEASE_SECONDS)
    token = uuid.uuid4().hex
    fields = {"owner": owner, "token": token, "acquiredAt": now, "leaseExpiresAt": expiry}
    from pymongo import ReturnDocument

    taken = db[LOCK_COLL].find_one_and_update(_expired_filter(now), {"$set": fields},
                                              return_document=ReturnDocument.AFTER)
    if taken is not None and taken.get("token") == token:
        return {"owner": owner, "token": token}
    try:
        db[LOCK_COLL].insert_one({"_id": "migrate", **fields})
        return {"owner": owner, "token": token}
    except Exception as exc:  # документ уже есть (свежий или гонка) — ещё раз попытка перехвата
        if "duplicate key" not in str(exc).lower() and "DuplicateKey" not in exc.__class__.__name__:
            raise
    taken = db[LOCK_COLL].find_one_and_update(_expired_filter(_utc_now()), {"$set": fields},
                                              return_document=ReturnDocument.AFTER)
    if taken is not None and taken.get("token") == token:
        return {"owner": owner, "token": token}
    return None


def heartbeat(db: Any, lock: dict) -> None:
    db[LOCK_COLL].update_one(
        {"_id": "migrate", "token": lock["token"]},
        {"$set": {"leaseExpiresAt": _utc_now() + timedelta(seconds=LOCK_LEASE_SECONDS)}},
    )


def release_lock(db: Any, lock: dict) -> None:
    db[LOCK_COLL].update_one({"_id": "migrate", "token": lock["token"]},
                             {"$set": {"leaseExpiresAt": _utc_now(), "owner": "released"}})


def migration_status(db: Any) -> dict[int, dict]:
    out: dict[int, dict] = {}
    try:
        for doc in db[MIG_COLL].find({}):
            out[int(doc["_id"])] = doc
    except Exception:
        pass
    return out


def _mark(db: Any, mig: Migration, status: str, report: dict | None, error: str | None, token: str) -> None:
    doc = db[MIG_COLL].find_one({"_id": mig.id}) or {"_id": mig.id, "name": mig.name}
    db[MIG_COLL].update_one(
        {"_id": mig.id},
        {"$set": {
            "name": mig.name, "status": status, "checksum": migration_checksum(mig),
            "leaseToken": token, "report": report or {}, "error": error,
            "startedAt": doc.get("startedAt") or (_utc_now() if status == "running" else None),
            "finishedAt": _utc_now() if status in ("done", "failed") else None,
        }},
        upsert=True,
    )


def latest_version(db: Any) -> int:
    try:
        doc = db[VERSION_COLL].find_one({"_id": "schema"})
    except Exception:
        return 0
    return int(doc.get("version", 0)) if doc else 0


# ------------------------------------------------------------------ up/plan


def plan_and_apply(db: Any, *, apply: bool, only: int | None = None,
                   owner: str = f"{socket.gethostname()}:{os.getpid()}") -> dict:
    statuses = migration_status(db)
    actions: list[dict] = []
    lock = None
    if apply:
        lock = acquire_lock(db, owner)
        if lock is None:
            raise RuntimeError("schema_lock занят другим runner'ом — повторить после истечения lease (DB04)")
    try:
        for mig in MIGRATIONS:
            if only is not None and mig.id != only:
                continue
            doc = statuses.get(mig.id)
            state = (doc or {}).get("status")
            if state == "done" and (doc or {}).get("checksum") == migration_checksum(mig):
                actions.append({"id": mig.id, "name": mig.name, "action": "skip-done"})
                continue
            if state == "done":  # контракт изменился без новой миграции — фиксируем, DDL не переигрываем
                actions.append({"id": mig.id, "name": mig.name, "action": "recheck-only"})
                continue
            if not apply:
                actions.append({"id": mig.id, "name": mig.name, "action": "would-apply"})
                continue
            _mark(db, mig, "running", None, None, lock["token"] if lock else "-")
            try:
                report = mig.apply(db, False)
                _mark(db, mig, "done", report, None, lock["token"] if lock else "-")
                actions.append({"id": mig.id, "name": mig.name, "action": "applied", "report": report})
            except Exception as exc:
                _mark(db, mig, "failed", None, f"{type(exc).__name__}: {exc}", lock["token"] if lock else "-")
                raise
        if apply:
            # post-apply строгая сверка: несовместимость спецификаций не скрывается (DB02)
            schema.verify_db(db, owners=("bot", "web", "shared")).raise_if_incompatible()
            db[VERSION_COLL].update_one(
                {"_id": "schema"},
                {"$set": {"version": SCHEMA_VERSION, "manifestChecksum": schema.manifest_checksum(),
                          "updatedAt": _utc_now()}},
                upsert=True,
            )
        return {"dryRun": not apply, "actions": actions,
                "schemaVersion": SCHEMA_VERSION if apply else latest_version(db)}
    finally:
        if lock is not None:
            release_lock(db, lock)


def check_rollback(db: Any, to_version: int) -> list[str]:
    """DB07 preflight: откат приложения на to_version блокируется, если выше
    применена backward-incompatible миграция (или она в running/failed)."""
    problems: list[str] = []
    for mig in MIGRATIONS:
        if mig.id <= to_version:
            continue
        doc = migration_status(db).get(mig.id)
        if not doc:
            continue
        if doc.get("status") in ("running", "failed"):
            problems.append(f"M{mig.id} {mig.name}: статус {doc['status']} — незавершённая миграция, rollback небезопасен")
        if doc.get("status") == "done" and not mig.backward_compatible:
            problems.append(
                f"M{mig.id} {mig.name}: backward-incompatible (уникальный индекс/сужение) — "
                f"rollback приложения на {to_version} заблокирован preflight'ом")
    return problems


# ------------------------------------------------------------------ users (DB06)


USER_PLAN: tuple[tuple[str, str, dict], ...] = (
    # (username, роль/привилегия, назначения)
    ("dsbot_app", "readWrite", "бот-сервисы: CRUD бизнес-коллекций, DDL недоступен"),
    ("dsbot_web", "readWrite", "web API: тот же уровень; операции идут через приложения"),
    ("dsbot_migration", "custom", "runner: createIndex/listIndexes/collMod + readWrite schema_*"),
    ("dsbot_backup", "backup", "мониторинг-юзер backup/restore (admin-роль Mongo)"),
)


def _already_exists(exc: Exception) -> bool:
    return getattr(exc, "code_name", "") == "AlreadyExists" or "already exists" in str(exc).lower()


def ensure_users(db: Any, *, passwords: dict[str, str]) -> list[str]:
    """Least-privilege пользователи (DB06). Идемпотентен: существующих не трогает.
    Роли: app/web — встроенная readWrite на свою БД (без createIndex — DDL не их),
    migration — кастомная роль с createIndex/listIndexes/collMod/read,
    backup — встроенная роль backup (admin). Сервер без --auth создаёт записи,
    но не enforcement: проверка отказа DDL — T13 на изолированном Mongo."""
    dbname = db.name
    made: list[str] = []
    try:
        db.command("createRole", "dsbot_migration_role",
                   privileges=[{"resource": {"db": dbname, "collection": ""},
                                "actions": ["createIndex", "listIndexes", "collMod", "find",
                                            "insert", "update", "remove"]}],
                   roles=[{"role": "read", "db": dbname}])
        made.append("role:dsbot_migration_role")
    except Exception as exc:
        if not _already_exists(exc):
            raise
    for username, kind, _note in USER_PLAN:
        pwd = passwords.get(username)
        if not pwd:
            continue
        roles = {
            "readWrite": [{"role": "readWrite", "db": dbname}],
            "custom": [{"role": "dsbot_migration_role", "db": dbname}],
            "backup": [{"role": "backup", "db": "admin"}],
        }[kind]
        try:
            db.command("createUser", username, pwd=pwd, roles=roles)
            made.append(f"user:{username}")
        except Exception as exc:
            if not _already_exists(exc):
                raise
    return made


# ------------------------------------------------------------------ CLI


def _connect(uri: str, db_name: str) -> tuple[Any, Any]:
    import pymongo

    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client, client[db_name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice_tracker.migrate")
    parser.add_argument("command", choices=["status", "plan", "up", "snapshot", "export-manifest",
                                            "users", "check-rollback"])
    parser.add_argument("--uri", default=os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27099"))
    parser.add_argument("--db", default=os.environ.get("MONGO_DB", "voice_tracker_t10_dev"))
    parser.add_argument("--only", type=int)
    parser.add_argument("--to-version", type=int)
    parser.add_argument("--out")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "export-manifest":
        if not args.out:
            parser.error("export-manifest требует --out")
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(schema.manifest_json())
        print(f"manifest checksum={schema.manifest_checksum()} → {args.out}")
        return 0

    client, db = _connect(args.uri, args.db)
    try:
        if args.command == "status":
            out = {"latest": latest_version(db), "migrations": migration_status(db),
                   "lock": db[LOCK_COLL].find_one({"_id": "migrate"})}
            print(json.dumps(out, indent=2, default=str, ensure_ascii=False))
        elif args.command == "plan":
            print(json.dumps(plan_and_apply(db, apply=False, only=args.only), indent=2, default=str, ensure_ascii=False))
        elif args.command == "up":
            if args.dry_run:
                print(json.dumps(plan_and_apply(db, apply=False, only=args.only), indent=2, default=str, ensure_ascii=False))
            else:
                print(json.dumps(plan_and_apply(db, apply=True, only=args.only), indent=2, default=str, ensure_ascii=False))
        elif args.command == "snapshot":
            snap = schema.snapshot_indexes(db, include_empty=True)
            text = json.dumps(snap, indent=2, default=str)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    fh.write(text)
                print(f"snapshot (индексы, без документов) → {args.out}")
            else:
                print(text)
        elif args.command == "users":
            passwords = {u: os.environ[f"DB_USER_{u.upper()}"] for u, _k, _n in USER_PLAN
                         if os.environ.get(f"DB_USER_{u.upper()}")}
            print(json.dumps({"created": ensure_users(db, passwords=passwords)}, ensure_ascii=False))
        elif args.command == "check-rollback":
            problems = check_rollback(db, args.to_version if args.to_version is not None else 0)
            print(json.dumps({"toVersion": args.to_version, "blocked": problems}, indent=2, ensure_ascii=False))
            return 2 if problems else 0
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
