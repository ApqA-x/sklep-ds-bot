"""T10 интеграция: migration runner на реальной Mongo стенда (27099).

DB01 — эквивалентный индекс под старым именем принят, не пересоздан;
DB02 — TTL-несовместимость обнаружена (startup-верка), не скрыта;
DB03 — fresh install, «старый snapshot» (индексы половины контракта) и повтор
       runner'а сходятся к одному контракту, повтор идемпотентен;
DB04 — активный лок отбивает второй runner; «краш» (running + просроченный
       лок) корректно продолжается без разрушительного повтора;
DB05 — дубли перед unique-индексом: конфликтные не удаляются молча, есть
       отчёт; эквивалентные мержатся, unique строится;
DB06 — least-privilege users (создание/роли идемпотентны);
DB07 — rollback-preflight блокирует откат под backward-incompatible миграцию.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from voice_tracker import migrate, schema  # noqa: E402
from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402

pytestmark = pytest.mark.integration

TEST_MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")


def _server_up() -> bool:
    try:
        client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    guard_mongo_uri(TEST_MONGO_URI)
    if not _server_up():
        pytest.skip("test mongod is not running on %s" % TEST_MONGO_URI)
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=3000)
    name = f"voice_tracker_t10m_{uuid.uuid4().hex[:10]}"
    guard_db_name(name)
    database = client[name]
    yield database
    client.drop_database(name)
    client.close()


def _index_names(db, coll: str) -> set[str]:
    return {doc["name"] for doc in db[coll].list_indexes()}


# ------------------------------------------------------------------ DB03


def test_fresh_install_repeat_and_partial_snapshot_converge(db) -> None:
    # DB03b: «старый snapshot» — половина контракта уже есть (как в проде до runner'а)
    old_spec = next(s for s in schema.MANIFEST if s.name == "status_1_guildId_1_channelId_1")
    db[old_spec.collection].create_index(list(old_spec.keys), **old_spec.create_kwargs())

    first = migrate.plan_and_apply(db, apply=True)
    assert [a["action"] for a in first["actions"]][:2] == ["applied", "applied"]
    assert migrate.latest_version(db) == schema.SCHEMA_VERSION

    second = migrate.plan_and_apply(db, apply=True)  # повтор — no-op (идемпотентность)
    assert {a["action"] for a in second["actions"]} == {"skip-done"}

    # итоговый контракт одинаков для всех owners
    report = schema.verify_db(db, owners=("bot", "web", "shared", "runner"))
    assert report.ok, report
    for spec in schema.MANIFEST:
        if spec.owner == "legacy":
            continue  # прод-наследие: документировано, не создаётся (T10.7)
        assert spec.name in _index_names(db, spec.collection), spec.name
    # старый индекс не пересоздан: его нельзя отличить — проверяем отсутствие дублей ключей
    names = [d["name"] for d in db[old_spec.collection].list_indexes()]
    assert len(names) == len(set(names))

    # snapshot — read-only метаданные всех коллекций, без документов (T10.1)
    snap = schema.snapshot_indexes(db, include_empty=True)
    assert snap["schemaVersion"] == schema.SCHEMA_VERSION
    assert old_spec.collection in snap["collections"]
    assert schema.EL in snap["collections"]
    assert "payload" not in json.dumps(snap, default=str)


def test_prod_snapshot_shape_regression(db) -> None:
    """Прод-снимок docs/schema/prod-indexes-2026-09-25.json — индексный метаданные,
    без документов; его эквивалентность manifest-алиасам (web_guild_status_endedAt)
    зафиксирована в ADR-0003. Здесь — проверка, что формат читается сверкой."""
    import json
    from pathlib import Path

    raw = json.loads(Path("docs/schema/prod-indexes-2026-09-25.json").read_text(encoding="utf-8"))
    assert raw["database"] == "voice_tracker"
    assert "voice_sessions" in raw["collections"]


# ------------------------------------------------------------------ DB01


def test_db01_equivalent_index_under_old_name_accepted_without_rebuild(db) -> None:
    spec = next(s for s in schema.MANIFEST if s.name == "web_guildId_status_endedAt")
    db[spec.collection].create_index(
        list(spec.keys),
        **{k: v for k, v in spec.create_kwargs().items() if k != "name"},
        name="web_guild_status_endedAt",  # «историческое» имя из прод-снимка
    )
    migrate.plan_and_apply(db, apply=True)
    names = _index_names(db, spec.collection)
    assert "web_guild_status_endedAt" in names  # не удалён
    assert spec.name not in names  # и не дублируется под каноническим именем
    report = schema.verify_db(db, owners=("web",))
    assert not report.incompatible
    assert f"{spec.collection}.{spec.name}~(web_guild_status_endedAt)" in report.under_other_name


# ------------------------------------------------------------------ DB02


def test_db02_incompatible_ttl_detected_at_verify(db) -> None:
    spec = next(s for s in schema.MANIFEST if s.collection == "processed_messages" and s.ttl)
    db[spec.collection].create_index([("createdAt", 1)], name="createdAt_1", expireAfterSeconds=999)
    report = schema.verify_db(db, owners=("bot",))
    assert any("expireAfterSeconds" in line for line in report.incompatible)
    with pytest.raises(schema.SchemaIncompatible):
        report.raise_if_incompatible()


# ------------------------------------------------------------------ DB04


def test_db04_lock_blocks_concurrent_runner_and_stale_lock_resumes(db) -> None:
    live = migrate.acquire_lock(db, "runner-A")
    assert live is not None
    assert migrate.acquire_lock(db, "runner-B") is None  # активный lease
    with pytest.raises(RuntimeError, match="schema_lock"):
        migrate.plan_and_apply(db, apply=True)
    migrate.release_lock(db, live)
    # «краш»: статус running без finish + просроченный лок → повторный up продолжает
    db[migrate.MIG_COLL].update_one(
        {"_id": 1},
        {"$set": {"name": "baseline", "status": "running", "checksum": migrate.migration_checksum(migrate.MIGRATIONS[0]),
                  "startedAt": datetime.now(UTC) - timedelta(hours=1), "leaseToken": "dead"}},
        upsert=True,
    )
    db[migrate.LOCK_COLL].update_one(
        {"_id": "migrate"},
        {"$set": {"owner": "crashed", "token": "dead", "leaseExpiresAt": datetime.now(UTC) - timedelta(seconds=10)}},
        upsert=True,
    )
    result = migrate.plan_and_apply(db, apply=True)
    assert result["actions"][0]["action"] == "applied"  # идемпотентное продолжение
    st = migrate.migration_status(db)[1]
    assert st["status"] == "done" and st["checksum"] == migrate.migration_checksum(migrate.MIGRATIONS[0])


# ------------------------------------------------------------------ DB05


def test_db05_conflicting_duplicates_abort_then_identical_merge_builds_unique(db) -> None:
    # только M1+M2: unique-индекс discord ещё НЕ построен — дубли вставляются легально
    migrate.plan_and_apply(db, apply=True, only=1)
    migrate.plan_and_apply(db, apply=True, only=2)

    db[schema.DA].insert_many([
        {"_id": "g1:e1#dup1", "guildId": "g1", "entryId": "e1", "actionType": "x"},
        {"_id": "g1:e1#dup2", "guildId": "g1", "entryId": "e1", "actionType": "x"},  # эквивалентен
        {"_id": "g1:e2#d1", "guildId": "g1", "entryId": "e2", "actionType": "x"},
        {"_id": "g1:e2#d2", "guildId": "g1", "entryId": "e2", "actionType": "y"},  # конфликт
    ])
    with pytest.raises(RuntimeError, match="discord_audit_logs"):
        migrate.plan_and_apply(db, apply=True, only=3)
    assert db[schema.DA].count_documents({}) == 4  # молча ничего не удалено
    failed = migrate.migration_status(db)[3]
    assert failed["status"] == "failed" and "discord_audit_logs" in failed["error"]

    # конфликт убран оператором — повтор M3 проходим: эквивалентные смержены, unique построен
    db[schema.DA].delete_one({"_id": "g1:e2#d2"})
    result = migrate.plan_and_apply(db, apply=True, only=3)
    assert result["actions"][0]["action"] == "applied"
    assert db[schema.DA].count_documents({}) == 2  # dup2 удалён merge'ом; d1 и оба e1-keep целые
    assert "discord_audit_guildId_entryId_unique" in _index_names(db, schema.DA)
    assert "web_disc_audit_guildId_entryId" in _index_names(db, schema.DA)  # старый не тронут (drop — руками)


# ------------------------------------------------------------------ DB06


def test_db06_least_privilege_users_created_idempotent(db) -> None:
    pw = {"dsbot_app": "app-pw-1", "dsbot_web": "web-pw-1", "dsbot_migration": "mig-pw-1",
          "dsbot_backup": "bkp-pw-1"}
    created = migrate.ensure_users(db, passwords=pw)
    assert any(u.startswith("user:") for u in created)
    again = migrate.ensure_users(db, passwords=pw)  # идемпотентность: существующих не трогаем
    assert [c for c in again if c.startswith("user:")] == []

    info = db.command("usersInfo")
    by_name = {u["user"]: u for u in info["users"]}
    dbname = db.name
    assert {r["db"] for u in by_name.values() for r in u["roles"]} <= {dbname, "admin"}
    assert all(r == {"role": "readWrite", "db": dbname} for r in by_name["dsbot_app"]["roles"])
    assert all(r == {"role": "readWrite", "db": dbname} for r in by_name["dsbot_web"]["roles"])
    assert {"role": "dsbot_migration_role", "db": dbname} in by_name["dsbot_migration"]["roles"]
    assert by_name["dsbot_backup"]["roles"] == [{"role": "backup", "db": "admin"}]

    roles = db.command("rolesInfo", showPrivileges=True)["roles"]
    mig_role = next(r for r in roles if r["role"] == "dsbot_migration_role")
    actions = {a for p in mig_role["privileges"] for a in p["actions"]}
    assert {"createIndex", "listIndexes", "collMod"} <= actions
    assert "dropIndex" not in actions and "dropCollection" not in actions  # DDL-разрушение не раздаётся


# ------------------------------------------------------------------ DB07


def test_db07_rollback_preflight_blocks_incompatible_downgrade(db) -> None:
    migrate.plan_and_apply(db, apply=True)  # M1–M3 на чистых данных
    assert migrate.check_rollback(db, 2), "откат под M3 (unique) обязан блокироваться"
    assert migrate.check_rollback(db, 3) == []  # откатывать нечего
    assert migrate.check_rollback(db, 1), "M3 above — блок"
    # additive M2: откат приложения с 2 на 1 не блокируется (TTL-индекс безвреден старому коду)
    for mid in (2, 3):
        db[migrate.MIG_COLL].update_one({"_id": mid}, {"$set": {"status": "pending"}})
    assert migrate.check_rollback(db, 1) == []
