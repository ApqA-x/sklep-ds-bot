"""T10: versioned migration runner для voice_tracker (единственный управляемый
шаг изменений схемы/данных). Приложения на startup только проверяют совместимость
(read-only, R26-07 review: ни DDL, ни записей); удаление и пересоздание индексов
И управляемые CRUD-backfills (M7 guild_settings.revision) — только здесь, под
migration-ролью.

Команды:
    python -m voice_tracker.migrate status              [--db NAME]
    python -m voice_tracker.migrate plan  [--only N]    # dry-run: что сделает
    python -m voice_tracker.migrate up    [--only N] [--dry-run]
    python -m voice_tracker.migrate snapshot --out FILE  # read-only, без документов
    python -m voice_tracker.migrate export-manifest --out FILE
    python -m voice_tracker.migrate users               # least-privilege (DB06)

На проде команды запускаются ОДНИМ и тем же compose-путём (профиль `migrate`,
сервис `schema-migrate`, credentials — MONGO_MIGRATION_URI пользователя
dsbot_migration), см. deploy/README.md и docs/adr/0005 — не из контейнера
приложения с runtime-URI.

Идемпотентность: повтор `up` на уже применённой версии — no-op (create_index с
той же спецификацией noop; CRUD-шаги фильтруются по $exists и не трогают
уже мигрированные документы; статус-документ не трогается). Смена манифеста без
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


def _apply_audit_state_index(db: Any, dry: bool) -> dict:
    """M4 (T11): unique (guildId) на discord_audit_state — инвариант «один документ
    состояния синхронизации аудита на гильдию». Коллекция новая, но precheck дублей
    read-only (как в M3): конфликт → abort, слепое удаление запрещено. Additive —
    старый код коллекцию не пишет, rollback приложения поверх M4 безопасен."""
    spec = next(s for s in MANIFEST if s.name == "discord_audit_state_guildId_unique")
    dups = _dup_groups(db, spec.collection, ["guildId"])
    if dups["conflicting"]:
        raise RuntimeError(
            f"discord_audit_state: {dups['conflicting']} гильдий с дублями состояния — "
            f"unique-индекс не строится, разбор вручную (ADR-0003). "
            f"Отчёт: {json.dumps(dups['groups'][:20], ensure_ascii=False, default=str)}"
        )
    if not dry:
        db[spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
    return {"index": f"{spec.collection}.{spec.name}", "duplicates": dups, "dryRun": dry}


def _apply_sweep_cursor_indexes(db: Any, dry: bool) -> dict:
    """M5 (R26-01): индексы курсорного sweep'а — forward-выборка (subject,createdAt,_id)
    и retry/oldest-выборки (consumer,state,createdAt). Аддитивны: старый код их не
    требует, новый деградирует в in-memory sort (верно, но дороже), rollback
    приложения безопасен."""
    created: list[str] = []
    for name in ("event_log_subject_createdAt_id", "event_inbox_consumer_state_createdAt"):
        spec = next(s for s in MANIFEST if s.name == name)
        if not dry:
            db[spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
        created.append(f"{spec.collection}.{spec.name}")
    return {"indexes": created, "dryRun": dry}


def _apply_ordering_gate_index(db: Any, dry: bool) -> dict:
    """M6 (R26-02): индекс гейта порядка доставки (subject, scope, seq) —
    предшественники scope читаются на каждой доставке. Аддитивен: гейт без
    индекса верен, но стоит дороже; rollback приложения безопасен."""
    spec = next(s for s in MANIFEST if s.name == "event_log_subject_scope_seq")
    if not dry:
        db[spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
    return {"index": f"{spec.collection}.{spec.name}", "dryRun": dry}


GUILD_SETTINGS = "guild_settings"


def _apply_guild_settings_revision_backfill(db: Any, dry: bool) -> dict:
    """M7 (R26-07 review, blocker 1): идемпотентный CRUD-backfill revision для
    старых документов guild_settings (существующих не трогает).

    Ранее этот же шаг выполнял startup рантайма (T06 в repository.verify_startup)
    — то есть скрытая миграция данных мимо runner'а и единственный write на
    старте. Startup теперь строго read-only (verify-only, DB03), и backfill живёт
    здесь: под migration-ролью, с lease-локом, статусом в schema_migrations и
    отчётом. Аддитивен: путь save_settings при revision==0/$exists:false
    работает и без него, rollback приложения безопасен."""
    flt = {"revision": {"$exists": False}}
    pending = db[GUILD_SETTINGS].count_documents(flt)
    if dry:
        return {"collection": GUILD_SETTINGS, "pending": pending, "dryRun": True}
    res = db[GUILD_SETTINGS].update_many(flt, {"$set": {"revision": 0}})
    return {"collection": GUILD_SETTINGS, "pending": pending,
            "backfilled": res.modified_count, "dryRun": False}


VOICE_PRESENCE_COLLECTION = "voice_presence_observations"
VOICE_SLEEP_TIMERS_COLLECTION = "voice_sleep_timers"


def _apply_voice_presence_collection(db: Any, dry: bool) -> dict:
    """M8: pre-create the observation collection under the DDL-capable role.

    Runtime credentials intentionally cannot implicitly create a collection.
    The built-in _id index is sufficient for the one-document-per-user key.
    """
    exists = VOICE_PRESENCE_COLLECTION in db.list_collection_names()
    if not dry and not exists:
        db.create_collection(VOICE_PRESENCE_COLLECTION)
    return {"collection": VOICE_PRESENCE_COLLECTION, "created": not exists and not dry, "dryRun": dry}


def _apply_sleep_timers_collection(db: Any, dry: bool) -> dict:
    """M9: pre-create timer state; indexed due scan arrives with its worker."""
    exists = VOICE_SLEEP_TIMERS_COLLECTION in db.list_collection_names()
    if not dry and not exists:
        db.create_collection(VOICE_SLEEP_TIMERS_COLLECTION)
    return {"collection": VOICE_SLEEP_TIMERS_COLLECTION, "created": not exists and not dry, "dryRun": dry}


def _apply_sleep_timer_due_index(db: Any, dry: bool) -> dict:
    spec = next(s for s in MANIFEST if s.name == "voice_sleep_status_due_id")
    if not dry:
        db[spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
    return {"index": f"{spec.collection}.{spec.name}", "dryRun": dry}


def _apply_sleep_timer_audit_index(db: Any, dry: bool) -> dict:
    spec = next(s for s in MANIFEST if s.name == "voice_sleep_audit_pending")
    if not dry:
        db[spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
    return {"index": f"{spec.collection}.{spec.name}", "dryRun": dry}


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline-index-contract", _apply_baseline, backward_compatible=True),
    Migration(2, "operations-journal-ttl", _apply_operations_ttl, backward_compatible=True),
    Migration(3, "discord-audit-entry-unique", _apply_discord_audit_unique, backward_compatible=False),
    Migration(4, "discord-audit-state-unique", _apply_audit_state_index, backward_compatible=True),
    Migration(5, "eventlog-sweep-cursor-indexes", _apply_sweep_cursor_indexes, backward_compatible=True),
    Migration(6, "eventlog-ordering-gate-index", _apply_ordering_gate_index, backward_compatible=True),
    Migration(7, "guild-settings-revision-backfill", _apply_guild_settings_revision_backfill,
              backward_compatible=True),
    Migration(8, "voice-presence-observations-collection", _apply_voice_presence_collection,
              backward_compatible=True),
    Migration(9, "voice-sleep-timers-collection", _apply_sleep_timers_collection,
              backward_compatible=True),
    Migration(10, "voice-sleep-timers-due-index", _apply_sleep_timer_due_index,
              backward_compatible=True),
    Migration(11, "voice-sleep-timers-audit-index", _apply_sleep_timer_audit_index,
              backward_compatible=True),
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


# ------------------------------------------------------------------ users (DB06/R26-07)

# R26-07 (V26-18): runtime-роли НЕ включают DDL: ни createIndex/dropIndex, ни
# dropCollection/createCollection, ни userAdmin. createCollection не нужен —
# сервер создаёт коллекции имплицитно при insert. Встроенная readWrite для
# app/web больше НЕ используется: она РЕАЛЬНО разрешает createIndex/dropIndex/
# dropCollection (проверено rolesInfo на Mongo 7), то есть ломала бы DB06.
# Единственный источник состава привилегий — ROLE_PLAN (тесты сверяют её
# декларативно, enforcement — интеграционный стенд tests/test_mongo_auth_stand.py).
# "getMore" в списке нет намеренно: это НЕ отдельная серверная привилегия —
# continuation курсора авторизуется теми же правами, что исходная операция
# чтения. Как отдельное action его не принимает createRole: живой прогон
# r2607-стенда на mongo:7 дал MongoServerError: Unrecognized action: getMore.
RUNTIME_ROLE_ACTIONS: tuple[str, ...] = (
    "find", "insert", "update", "remove",
    "listCollections", "listIndexes", "collStats", "dbStats", "killCursors",
)
# Отличие runner'а от runtime — ровно DDL индексов и коллекций: пересборка
# индекса = dropIndex + createIndex, implicit-create новой коллекции требует
# createCollection, сверка/смена опций коллекции — collMod. Ни dropCollection,
# ни dropDatabase, ни userAdmin/grantRole/revokeRole: удаление коллекций —
# ручной шаг оператора после разбора (DB05), гранты — не дело runner'а.
# "aggregate" здесь был бы ошибкой ПЛАНА, а не недостающей привилегией: в
# модели авторизации Mongo отдельного action `aggregate` НЕТ — агрегация,
# которая только читает, авторизуется правом `find` на коллекции (createRole на
# mongo:7 отвергает "aggregate" так же, как "getMore": Unrecognized action,
# живой прогон стенда 2026-09-27). Тем же `find` из RUNTIME_ROLE_ACTIONS
# покрыты серверная агрегация в _dup_groups (M3/M4) и count_documents в M7 —
# миграционной роли отдельное право на чтение не нужно, а custom-роль отдаёт
# серверу ровно переданный список, поэтому «на всякий случай» его расширять
# нельзя: лишний action — это либо падение createRole, либо незадекларированное
# расширение прав (DB06).
MIGRATION_ROLE_EXTRA: tuple[str, ...] = (
    "createIndex", "dropIndex", "createCollection", "collMod",
)

# Кастомные роли (создаются в рабочей БД): имя -> ровно набор actions.
ROLE_PLAN: dict[str, tuple[str, ...]] = {
    "dsbot_runtime_bot_role": RUNTIME_ROLE_ACTIONS,
    "dsbot_runtime_web_role": RUNTIME_ROLE_ACTIONS,
    "dsbot_migration_role": RUNTIME_ROLE_ACTIONS + MIGRATION_ROLE_EXTRA,
}

# (username, роль, назначения). db="{dbname}" — плейсхолдер рабочей БД;
# built-in роли (backup/restore/readAnyDatabase) живут в admin (DB06), НО все
# пользователи плана (включая dsbot_backup/dsbot_restore) создаются в рабочей
# БД: ensure_users работает на client[db_name]. authSource их URI — MONGO_DB,
# не admin (review R26-07: admin в env-шаблонах = Authentication failed на
# живом mongod; роль в admin не переносит туда пользователя).
USER_PLAN: tuple[tuple[str, tuple[tuple[str, str], ...], str], ...] = (
    ("dsbot_app", (("dsbot_runtime_bot_role", "{dbname}"),),
     "бот-сервисы: CRUD бизнес-коллекций, DDL недоступен (R26-07)"),
    ("dsbot_web", (("dsbot_runtime_web_role", "{dbname}"),),
     "web API: тот же уровень без DDL; операции идут через приложения"),
    ("dsbot_migration", (("dsbot_migration_role", "{dbname}"),),
     "runner `migrate up`: CRUD + createIndex/dropIndex/createCollection/collMod"),
    ("dsbot_backup", (("backup", "admin"),),
     "mongodump в backup.sh (встроенная роль backup)"),
    ("dsbot_restore", (("restore", "admin"), ("readAnyDatabase", "admin")),
     "mongorestore + гейты restore.sh: listDatabases/чтение целей (R26-07)"),
)

# Состав bootstrap root (localhost exception) — ЕДИНСТВЕННЫЙ источник истины
# и для bootstrap_users ниже, и для генератора стенда deploy/scripts/
# r2607_auth_stand.sh (импортирует этот же констант): стенд не может «прятать»
# расхождение с продакционной комплектацией (review R26-07, blocker 2:
# bootstrap_users создавал root только с userAdminAnyDatabase, чего не хватает
# ни на createRole по плану, ни на штатный restore-cleanup dropDatabase).
# root — НЕ runtime-credential: только `migrate users --bootstrap`/`migrate
# users` (репарация grants) и mongosh-гейты restore.sh. Учтены granter-ограничения
# Mongo (эмпирически подтверждены живым прогоном стенда на mongo:7):
#   * userAdminAnyDatabase — createUser/grantRole/createRole на любой БД (ensure_users);
#   * readWriteAnyDatabase — привилегии find/insert/update/remove, которыми грантер
#     обязан владеть для createRole кастомных ролей плана (CRUD-часть);
#   * dbAdminAnyDatabase  — createIndex/dropIndex/createCollection/collMod
#     (createRole migration-роли) + dropDatabase: у встроенной dbAdmin на mongo:7
#     есть это действие (docs v7.0, список actions dbAdmin) — им делает cleanup
#     таргет-БД restore.sh под MONGO_ADMIN_URI;
#   * backup / restore    — выдача тех же ролей dsbot_backup/dsbot_restore без
#     Unauthorized (грантер владеет ролью) + listDatabases-гейт restore.sh;
#   * clusterMonitor      — кластерная диагностика для тех же admin-гейтов.
# Комплектация закреплена unit-тестом точного состава
# (tests/test_mongo_roles_plan.py::TestRootRolePlan) и доказывается живым
# стендом (tests/test_mongo_auth_stand.py: dropDatabase root'а, bootstrap_users
# поверх стендового кластера, auth backup/restore URI).
ROOT_ROLE_PLAN: tuple[tuple[str, str], ...] = (
    ("userAdminAnyDatabase", "admin"),
    ("readWriteAnyDatabase", "admin"),
    ("dbAdminAnyDatabase", "admin"),
    ("backup", "admin"),
    ("restore", "admin"),
    ("clusterMonitor", "admin"),
)


def root_roles_doc() -> list[dict]:
    """ROOT_ROLE_PLAN в форме серверного документа createUser.roles."""
    return [{"role": role, "db": db} for role, db in ROOT_ROLE_PLAN]


def _already_exists(exc: Exception) -> bool:
    return getattr(exc, "code_name", "") == "AlreadyExists" or "already exists" in str(exc).lower()


def _unauthorized(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 13 or getattr(exc, "code_name", "") == "Unauthorized" \
        or "unauthorized" in str(exc).lower()


def _desired_roles(username: str, dbname: str) -> list[dict]:
    entry = next((u for u in USER_PLAN if u[0] == username), None)
    if entry is None:
        raise ValueError(f"пользователь {username!r} вне USER_PLAN")
    return [{"role": role, "db": db if db != "{dbname}" else dbname} for role, db in entry[1]]


def _role_privileges(role: str, dbname: str) -> list[dict]:
    return [{"resource": {"db": dbname, "collection": ""}, "actions": sorted(ROLE_PLAN[role])}]


def ensure_roles(db: Any) -> list[str]:
    """Кастомные роли из ROLE_PLAN: создать, а при дрейфе состава — ПЕРЕЗАПИСАТЬ
    (updateRole заменяет privileges и roles целиком). Молча принимать чужой
    набор (например leftover createIndex у runtime-роли) нельзя — это=DB06 (R26-07)."""
    dbname = db.name
    made: list[str] = []
    for role in ROLE_PLAN:
        privileges = _role_privileges(role, dbname)
        try:
            db.command("createRole", role, privileges=privileges, roles=[])
            made.append(f"role:{role}")
            continue
        except Exception as exc:
            if not _already_exists(exc):
                raise
        info = db.command("rolesInfo", [{"role": role, "db": dbname}], showPrivileges=True)
        docs = [r for r in info.get("roles", []) if r.get("role") == role and r.get("db") == dbname]
        if docs and _role_matches_plan(docs[0], role, dbname):
            continue
        db.command("updateRole", role, privileges=privileges, roles=[])
        made.append(f"role-repaired:{role}")
    return made


def _role_matches_plan(doc: Any, role: str, dbname: str) -> bool:
    """Состав роли ровно как в ROLE_PLAN: один privilege на (db, "") с точным
    набором actions, без унаследованных ролей (иначе leftover readWrite утёк бы
    в привилегии мимо плана)."""
    if list(doc.get("roles") or []):
        return False
    expected_actions = set(ROLE_PLAN[role])
    privileges = doc.get("privileges") or []
    if len(privileges) != 1:
        return False
    res = privileges[0].get("resource") or {}
    if res.get("db") != dbname or res.get("collection") != "":
        return False
    return set(privileges[0].get("actions") or []) == expected_actions


def _users_info(db: Any) -> dict[str, dict]:
    info = db.command("usersInfo")
    return {u["user"]: u for u in info.get("users", [])}


def ensure_users(db: Any, *, passwords: dict[str, str]) -> list[str]:
    """Least-privilege пользователи (DB06) ЯВНОЙ сверкой grants (R26-07).

    Идемпотентность усиленная: после createUser/upsert-пути роли существующих
    пользователей сверяются через usersInfo, а состав кастомных ролей — через
    rolesInfo. Избыточные роли (например leftover встроенной readWrite с её
    createIndex/dropIndex/dropCollection) — ОТЗЫВАЮТСЯ (revokeRolesFromUser),
    недостающие выдаются (grantRolesToUser); серверных команд grantRoles/
    revokeRoles не существует, имя пользователя передаётся первым позиционным
    аргументом. Съехавший состав роли — чинится updateRole. Молчать
    про drift нельзя: без этого шага DB06 держится только на честном слове.
    Локаут-безопасно: изменяются ТОЛЬКО пользователи плана dsbot_*; root/admin
    никогда не понижаются (их нет в USER_PLAN, а реверк идёт поимённо).
    Пароли существующих пользователей не ротируются (ротация — отдельный шаг
    оператора: updateUser pwd + смена URI в env-файле)."""
    dbname = db.name
    made: list[str] = ensure_roles(db)
    existing = _users_info(db)
    for username, _roles, _note in USER_PLAN:
        desired = _desired_roles(username, dbname)
        want = {(r["role"], r["db"]) for r in desired}
        have = {(r["role"], r["db"]) for r in (existing.get(username) or {}).get("roles", [])}
        if username not in existing:
            pwd = passwords.get(username)
            if not pwd:
                continue  # секрета нет — создать нельзя; это не секрет, имя печатать можно
            try:
                db.command("createUser", username, pwd=pwd, roles=desired)
                made.append(f"user:{username}")
                have = want
            except Exception as exc:
                if not _already_exists(exc):
                    raise
        excess = sorted(have - want)
        missing = sorted(want - have)
        if excess:
            db.command("revokeRolesFromUser", username,
                       roles=[{"role": r, "db": d} for r, d in excess])
            made.extend(f"revoke:{username}:{r}" for r, _d in excess)
        if missing:
            db.command("grantRolesToUser", username,
                       roles=[{"role": r, "db": d} for r, d in missing])
            made.extend(f"grant:{username}:{r}" for r, _d in missing)
    return made


def bootstrap_users(*, local_uri: str, db_name: str, passwords: dict[str, str],
                    root_user: str, root_pass: str,
                    admin_uri: str = "") -> tuple[list[str], str]:
    """`migrate users --bootstrap` (R26-07 шаг 1): первый пользователь на ПУСТОМ
    mongod --auth через localhost exception (подключение с localhost без
    credentials создаёт первого админа), затем роли+пользователи плана под ним.

    Состав root — строго ROOT_ROLE_PLAN (та же константа, из которой генерится
    r2607-стенд): userAdminAnyDatabase одна не покрывает ни createRole ролей
    плана (грантер обязан владеть выдаваемыми привилегиями), ни dropDatabase
    штатного restore-cleanup (review R26-07, blocker 2).

    Идемпотентность: если localhost exception уже закрыта (Unauthorized 13 —
    на кластере есть пользователи), подключаемся admin-URI (явный MONGO_ADMIN_URI
    или собранный из root-учётки) и продолжаем обычный ensure_users со сверкой
    grants. Секреты не возвращаются и не печатаются — только имена созданных."""
    import urllib.parse

    import pymongo

    note = "bootstrap:root-created"
    try:
        client = pymongo.MongoClient(local_uri, serverSelectionTimeoutMS=5000)
        try:
            client.admin.command("createUser", root_user, pwd=root_pass,
                                 roles=root_roles_doc())
        finally:
            client.close()
    except Exception as exc:
        if not (_unauthorized(exc) or _already_exists(exc)):
            raise
        note = "bootstrap:admin-auth"  # exception закрыта или root уже есть — идемпотентно

    uri = admin_uri
    if not uri:
        loc = urllib.parse.urlparse(local_uri)
        netloc = f"{urllib.parse.quote_plus(root_user)}:{urllib.parse.quote_plus(root_pass)}@{loc.hostname}:{loc.port or 27017}"
        uri = f"mongodb://{netloc}/admin?authSource=admin"
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        made = ensure_users(client[db_name], passwords=passwords)
    finally:
        client.close()
    return ([note] + made), note


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
    parser.add_argument("--bootstrap", action="store_true",
                        help="users: пустой mongod --auth через localhost exception (R26-07)")
    args = parser.parse_args(argv)

    if args.command == "export-manifest":
        if not args.out:
            parser.error("export-manifest требует --out")
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(schema.manifest_json())
        print(f"manifest checksum={schema.manifest_checksum()} → {args.out}")
        return 0

    if args.command == "users" and args.bootstrap:
        root_user = os.environ.get("DB_USER_ROOT", "").strip()
        root_pass = os.environ.get("DB_PASS_ROOT", "")
        if not root_user or not root_pass:
            # имена переменных — не секрет; значения не печатаем никогда
            parser.error("users --bootstrap требует DB_USER_ROOT и DB_PASS_ROOT в окружении")
        passwords = {u: os.environ[f"DB_USER_{u.upper()}"] for u, _r, _n in USER_PLAN
                     if os.environ.get(f"DB_USER_{u.upper()}")}
        made, _note = bootstrap_users(
            local_uri=args.uri, db_name=args.db, passwords=passwords,
            root_user=root_user, root_pass=root_pass,
            admin_uri=os.environ.get("MONGO_ADMIN_URI", "").strip(),
        )
        # только имена созданных/починенных сущностей — без секретов (DB06)
        print(json.dumps({"created": made}, ensure_ascii=False))
        return 0
    if args.bootstrap:
        parser.error("--bootstrap применим только к users")

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
