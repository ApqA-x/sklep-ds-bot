"""R26-07: enforcement-проверка модели прав (DB06) на изолированном mongod --auth.

До R26-07 роль runtime-пользователей (readWrite) декларировалась планом, но не
доказывалась: без --auth сервер вообще не сверяет привилегии, а readWrite
реально разрешает createIndex/dropIndex/dropCollection. Здесь доказательства
реальными отказами сервера:

  * runtime-пользователи (dsbot_app/dsbot_web) делают CRUD и listIndexes, но
    ЛЮБОЙ DDL/админ-команду сервер отбивает кодом 13 (Unauthorized);
  * созидательный DDL доступен только dsbot_migration;
  * у dsbot_app в authenticatedUserRoles нет ни одной админской роли;
  * grants-репарация ensure_users отзывает leftover-роль readWrite у
    пользователя плана (DB06-нарушение прошлого) и НЕ трогает пользователей
    вне плана (локаут-безопасность реверка поимённо).

Стенд одноразовый (mongo:7 --auth, 127.0.0.1:27098, БД
voice_tracker_t07auth_<hex>), роли/пользователи — из ROLE_PLAN/USER_PLAN:

    eval "$(deploy/scripts/r2607_auth_stand.sh up)"
    python -B -m pytest tests/test_mongo_auth_stand.py -q -m integration
    deploy/scripts/r2607_auth_stand.sh --down

Без URI в окружении или при недоступном стенде — skip. guard_mongo_uri/
guard_db_name вызываются ПЕРЕД подключением: чужой/продовой URI или имя БД —
AssertionError (падает, а не скипается).
"""
from __future__ import annotations

import os
import uuid

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402
from pymongo import errors as pymongo_errors  # noqa: E402

from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402
from voice_tracker import migrate, schema  # noqa: E402

pytestmark = pytest.mark.integration

ENV_URI = {
    "admin": "TEST_MONGO_ADMIN_URI",
    "app": "TEST_MONGO_APP_URI",
    "web": "TEST_MONGO_WEB_URI",
    "migration": "TEST_MONGO_MIGRATION_URI",
}
RUNTIME_ADMIN_ROLES = {
    "admin", "root", "__system", "userAdmin", "userAdminAnyDatabase",
    "dbAdmin", "dbAdminAnyDatabase", "read", "readAnyDatabase",
    "readWrite", "readWriteAnyDatabase", "backup", "restore",
    "clusterAdmin", "clusterMonitor",
}


def _db_name() -> str:
    name = os.environ.get("TEST_MONGO_DB", "").strip()
    if not name:
        pytest.skip("TEST_MONGO_DB не задан — подними стенд: eval \"$(deploy/scripts/r2607_auth_stand.sh up)\"")
    guard_db_name(name)
    return name


def _client(kind: str) -> MongoClient:
    env_key = ENV_URI[kind]
    uri = os.environ.get(env_key, "").strip()
    if not uri:
        pytest.skip(f"{env_key} не задан — подними стенд: eval \"$(deploy/scripts/r2607_auth_stand.sh up)\"")
    # guard — СТРОГО до любого подключения (fail-closed на прод-ресурсы)
    guard_mongo_uri(uri)
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        # pymongo аутентифицирует каждое соединение: ping подтверждает и
        # доступность, и валидность credentials
        client.admin.command("ping")
    except Exception as exc:
        pytest.skip(f"auth-стенд недоступен по {env_key}: {type(exc).__name__}")
    return client


@pytest.fixture(scope="module")
def db_name() -> str:
    return _db_name()


@pytest.fixture(scope="module")
def admin_client(db_name):
    client = _client("admin")
    yield client
    # уборка стендовой БД — ОДИН dropDatabase в teardown модуля (brief: через
    # admin-клиент). По-тестовые dropDatabase запрещены structurally: вместе с
    # базой улетят и пользователи с authSource=этой БД, а следующие тесты
    # получили бы «user not found» вместо доказательства отказа ролью.
    # Разделение тестов — именами коллекций/документов с uuid.
    try:
        client.drop_database(db_name)
    finally:
        client.close()


@pytest.fixture(scope="module")
def collections_ready(admin_client, db_name):
    """Коллекции контракта создаёт admin (DDL — не право runtime; в штатном
    прогоне их создаёт `migrate up`). Без этого первый insert runtime-юзера
    проверял бы implicit-create, а не CRUD по существующей коллекции."""
    db = admin_client[db_name]
    for coll in (schema.S, schema.WA):
        try:
            db.create_collection(coll)
        except pymongo_errors.CollectionInvalid:
            pass  # уже есть (идемпотентность при повторе модуля)
    return db


@pytest.fixture()
def app_db(db_name):
    client = _client("app")
    yield client[db_name]
    client.close()


@pytest.fixture()
def web_db(db_name):
    client = _client("web")
    yield client[db_name]
    client.close()


@pytest.fixture()
def migration_db(db_name):
    client = _client("migration")
    yield client[db_name]
    client.close()


def _denied_code(excinfo, what: str) -> None:
    """Доказательство DB06 — РЕАЛЬНЫЙ код сервера 13 (Unauthorized), а не
    «просто упало» (иначе отказ могли дать NamespaceNotFound/таймаут)."""
    err = excinfo.value
    assert err.code == 13, f"{what}: ожидался code 13 (Unauthorized), получен {err.code}/{err.code_name}"


# ------------------------------------------------------- runtime: CRUD разрешён


def test_runtime_crud_ok(collections_ready, app_db, web_db) -> None:
    marker = uuid.uuid4().hex
    doc = {"_id": f"t07app-{marker}", "guildId": "1", "status": "ended", "n": 1}
    app_db[schema.S].insert_one(doc)
    assert app_db[schema.S].find_one({"_id": doc["_id"]})["n"] == 1
    app_db[schema.S].update_one({"_id": doc["_id"]}, {"$set": {"n": 2}})
    assert app_db[schema.S].find_one({"_id": doc["_id"]})["n"] == 2
    assert app_db[schema.S].delete_one({"_id": doc["_id"]}).deleted_count == 1

    wdoc = {"_id": f"t07web-{marker}", "guildId": "1", "at": marker}
    web_db[schema.WA].insert_one(wdoc)
    assert web_db[schema.WA].find_one({"_id": wdoc["_id"]})["at"] == marker
    web_db[schema.WA].update_one({"_id": wdoc["_id"]}, {"$set": {"at": marker + "x"}})
    assert web_db[schema.WA].find_one({"_id": wdoc["_id"]})["at"] == marker + "x"
    assert web_db[schema.WA].delete_one({"_id": wdoc["_id"]}).deleted_count == 1


# --------------------------------------------------------- runtime: DDL запрещён


def test_runtime_ddl_denied(collections_ready, app_db) -> None:
    with pytest.raises(pymongo_errors.OperationFailure) as exc:
        app_db[schema.S].create_index([("t07_forbidden", 1)], name="t07_forbidden")
    _denied_code(exc, "createIndex")
    with pytest.raises(pymongo_errors.OperationFailure) as exc:
        app_db[schema.S].drop_index("anything_1")
    _denied_code(exc, "dropIndexes")
    with pytest.raises(pymongo_errors.OperationFailure) as exc:
        app_db[schema.WA].drop()
    _denied_code(exc, "dropCollection")
    with pytest.raises(pymongo_errors.OperationFailure) as exc:
        app_db.command("createUser", f"t07evil_{uuid.uuid4().hex[:8]}", pwd="x" * 12, roles=[])
    _denied_code(exc, "createUser")


def test_runtime_can_list_indexes(collections_ready, app_db, web_db) -> None:
    # listIndexes — read-привилегия плана RUNTIME_ROLE_ACTIONS: обязана быть
    names = {ix["name"] for ix in app_db[schema.S].list_indexes()}
    assert "_id_" in names
    assert "_id_" in {ix["name"] for ix in web_db[schema.WA].list_indexes()}


# ------------------------------------------------ migration: созидательный DDL


def test_migration_can_create_index(collections_ready, migration_db) -> None:
    name = f"t07mig_{uuid.uuid4().hex[:8]}"
    assert migration_db[schema.S].create_index([("n", 1)], name=name) == name
    assert name in {ix["name"] for ix in migration_db[schema.S].list_indexes()}


# -------------------------------------------------------------- app — не админ


def test_app_not_admin(collections_ready, app_db, db_name) -> None:
    status = app_db.command("connectionStatus")
    auth_roles = {(r["role"], r["db"]) for r in status["authInfo"]["authenticatedUserRoles"]}
    assert ("dsbot_runtime_bot_role", db_name) in auth_roles
    assert not ({role for role, _db in auth_roles} & RUNTIME_ADMIN_ROLES), auth_roles
    # usersInfo на admin — отказ (у app нет viewUser ни на какой admin-ресурс)
    with pytest.raises(pymongo_errors.OperationFailure) as exc:
        app_db.client["admin"].command("usersInfo")
    _denied_code(exc, "usersInfo@admin")


# ------------------------------------------------------- grants-репарация (DB06)


def test_grants_repair(admin_client, collections_ready, db_name) -> None:
    """ensure_users (импорт из voice_tracker.migrate) вызывается от admin-сессии
    к базе стенда (client=admin_uri[db]) — ровно как в `migrate users`."""
    db = admin_client[db_name]
    tmp = f"t07tmp_{uuid.uuid4().hex[:8]}"
    try:
        # (1) пользователь ВНЕ плана: локаут-безопасность — реверк поимённо,
        # чужих ensure_users не трогает (в т.ч. root вне USER_PLAN)
        db.command("createUser", tmp, pwd="t07" + uuid.uuid4().hex, roles=[{"role": "readWrite", "db": db_name}])
        made = migrate.ensure_users(db, passwords={})
        assert not [m for m in made if tmp in m], made
        users = {u["user"] for u in db.command("usersInfo")["users"]}
        assert tmp in users

        # (2) DB06-нарушение прошлого: leftover встроенной readWrite у runtime-
        # пользователя плана (именно её до R26-07 давал `migrate users`) —
        # ensure_users обязан ОТЗВАТЬ, оставив состав ровно по USER_PLAN
        db.command("grantRolesToUser", "dsbot_app", roles=[{"role": "readWrite", "db": db_name}])
        drifted = db.command("usersInfo", "dsbot_app")["users"][0]["roles"]
        assert {"role": "readWrite", "db": db_name} in drifted, drifted
        made = migrate.ensure_users(db, passwords={})
        assert "revoke:dsbot_app:readWrite" in made, made
        roles = db.command("usersInfo", "dsbot_app")["users"][0]["roles"]
        assert roles == [{"role": "dsbot_runtime_bot_role", "db": db_name}], roles
    finally:
        try:
            db.command("dropUser", tmp)
        except Exception as exc:  # cleanup-diagnostic: не меняем исходный отказ
            print(f"WARN: dropUser {tmp} не удался: {type(exc).__name__}")
