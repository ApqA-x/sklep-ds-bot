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
    вне плана (локаут-безопасность реверка поимённо);
  * review R26-07, blocker 1: backup/restore-URI стенда (TEST_MONGO_BACKUP_URI/
    TEST_MONGO_RESTORE_URI) РЕАЛЬНО аутентифицируются с authSource=<рабочая БД>
    (пользователи созданы в ней, built-in роли лишь выданы из admin) и дают
    ровно роли плана; та же учётка через authSource=admin сервер ОТВЕРГАЕТ —
    ровно тот режим отказа, который старые env-шаблоны ловили уже на проде;
  * review R26-07, blocker 2: root на живом кластере создан ТОЧНОЙ
    production-командой `migrate users --bootstrap` (см. r2607_auth_stand.sh),
    его authenticatedUserRoles на сервере равны ROOT_ROLE_PLAN, root делает
    dropDatabase (штатный restore-cleanup гейт restore.sh), а повторный прогон
    production bootstrap_users() поверх живого кластера идемпотентен;
  * review R26-07, blocker 2 (экспорт): deploy-equivalent прогон runner'а
    доказывается наружу несекретными маркерами `R2607_*` из eval-блока стенда —
    rc'ы `migrate up`/`status`, число применённых шагов, статус/результат
    backfill-шага M7, latest vs SCHEMA_VERSION и отказ безпарольного CLI.
    Маркеры обязаны совпадать с сервером (иначе «доказательство» было бы
    доверием напечатанному тексту).

Стенд одноразовый (mongo:7 --auth, 127.0.0.1:27098, БД
voice_tracker_t07auth_<hex>), роли/пользователи — из ROLE_PLAN/USER_PLAN:

    eval "$(deploy/scripts/r2607_auth_stand.sh up)"
    python -B -m pytest tests/test_mongo_auth_stand.py -q -m integration
    deploy/scripts/r2607_auth_stand.sh --down

Без URI в окружении или при недоступном стенде — skip. При ПОДНЯТОМ стенде
(TEST_MONGO_DB задан) отсутствующий URI-экспорт или отсутствующий маркер
R2607_* — FAIL, не skip: живые проверки обязательных URI и deploy-equivalent
прогона runner'а не должны «испаряться» молча (review R26-07, blocker 1/2).
guard_mongo_uri/guard_db_name вызываются ПЕРЕД подключением:
чужой/продовой URI или имя БД — AssertionError (падает, а не скипается).
"""
from __future__ import annotations

import os
import re
import uuid
from urllib.parse import unquote, urlparse

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
    # review R26-07 (blocker 1): эти два URI — обязательная часть живого стенда
    "backup": "TEST_MONGO_BACKUP_URI",
    "restore": "TEST_MONGO_RESTORE_URI",
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


def _uri(env_key: str) -> str:
    """URI стенда. Нет стенда (TEST_MONGO_DB не задан) — skip; стенд ПОДНЯТ, а
    обязательного export нет — FAIL: набор URI из r2607_auth_stand.sh — часть
    живого контракта (review R26-07, blocker 1), «забытый» backup/restore URI
    не должен превращать проверку в тихий skip."""
    uri = os.environ.get(env_key, "").strip()
    if uri:
        return uri
    if os.environ.get("TEST_MONGO_DB", "").strip():
        pytest.fail(f"стенд поднят (TEST_MONGO_DB задан), но {env_key} отсутствует: "
                    "r2607_auth_stand.sh обязан экспортировать ВСЕ URI плана — "
                    'переподними: eval "$(deploy/scripts/r2607_auth_stand.sh up)"')
    pytest.skip(f"{env_key} не задан — подними стенд: eval \"$(deploy/scripts/r2607_auth_stand.sh up)\"")


def _client(kind: str) -> MongoClient:
    env_key = ENV_URI[kind]
    uri = _uri(env_key)
    # guard — СТРОГО до любого подключения (fail-closed на прод-ресурсы)
    guard_mongo_uri(uri)
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        # pymongo аутентифицирует каждое соединение (handshake SASL): ping
        # подтверждает и доступность, и валидность credentials
        client.admin.command("ping")
    except pymongo_errors.OperationFailure as exc:
        # сервер ОТВЕТИЛ, но отверг учётные данные/полномочия — это ровно
        # режим отказа сломанного authSource (blocker 1). Skip здесь «спрятал»
        # бы проверку: живый тест обязан падать.
        client.close()
        pytest.fail(f"{env_key}: живые учётные данные отвергнуты сервером "
                    f"(code {exc.code}/{exc.code_name}) — регрессия контракта "
                    "authSource/пароля, а не недоступный стенд")
    except Exception as exc:
        client.close()
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


# --------------------------------------- blocker 1: backup/restore URI — живая auth


def _auth_roles(client: MongoClient, db: str) -> set[tuple[str, str]]:
    status = client[db].command("connectionStatus")
    return {(r["role"], r["db"]) for r in status["authInfo"]["authenticatedUserRoles"]}


def test_backup_uri_live_auth_and_exact_roles(db_name) -> None:
    """TEST_MONGO_BACKUP_URI реально проходит SASL на mongo:7 (authSource=рабочая
    БД) и даёт ровно роли плана USER_PLAN — ничего больше. До R26-07 env-шаблоны
    ставили authSource=admin (пользователя там нет) — падало уже на проде;
    _client() обязан УМЕРЕТЬ, а не смиться, если стенд это «забыл»."""
    client = _client("backup")
    try:
        assert _auth_roles(client, db_name) == {("backup", "admin")}
    finally:
        client.close()


def test_restore_uri_live_auth_and_exact_roles(db_name) -> None:
    client = _client("restore")
    try:
        assert _auth_roles(client, db_name) == {("restore", "admin"),
                                                ("readAnyDatabase", "admin")}
    finally:
        client.close()


def test_backup_restore_reject_admin_authsource(db_name) -> None:
    """Ровно режим отказа старых env-шаблонов: та же учётка с authSource=admin
    сервер ОТВЕРГАЕТ (AuthenticationFailed 18). Пользователи плана живут в
    рабочей БД; built-in роль в admin это не переносит (review R26-07, blocker 1)."""
    for kind in ("backup", "restore"):
        # _uri принимает ИМЯ ENV-КЛЮЧА (не имя плана): без ENV_URI[kind] здесь
        # читался бы os.environ["backup"] -> FAIL «стенд поднят, но backup
        # отсутствует» вместо живой проверки отказа
        uri = _uri(ENV_URI[kind]).replace(f"authSource={db_name}", "authSource=admin")
        assert "authSource=admin" in uri, "стендовый URI должен меняться предсказуемо"
        guard_mongo_uri(uri)
        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        try:
            with pytest.raises(pymongo_errors.OperationFailure) as exc:
                client.admin.command("ping")
            assert exc.value.code == 18, f"{kind}: ожидался AuthenticationFailed, " \
                f"получен {exc.value.code}/{exc.value.code_name}"
        finally:
            client.close()


# ------------------------------- blocker 2: root == ROOT_ROLE_PLAN на живом сервере


def test_root_roles_equal_plan_on_server(admin_client) -> None:
    """authenticatedUserRoles root'а НА СЕРВЕРЕ равны ROOT_ROLE_PLAN — состав
    доказан production-путём (стенд поднимал права `migrate users --bootstrap`,
    а не самописным mongosh-генератором)."""
    assert _auth_roles(admin_client, "admin") == set(migrate.ROOT_ROLE_PLAN)


def test_root_can_drop_database(admin_client) -> None:
    """Штатный restore-cleanup гейт restore.sh (dropDatabase таргета под
    MONGO_ADMIN_URI): dbAdminAnyDatabase в ROOT_ROLE_PLAN обязана работать на
    живом mongo:7 — иначе cleanup невозможнен и оператор уйдёт в ручные rm."""
    scratch = f"voice_tracker_t07drop_{uuid.uuid4().hex[:12]}"
    guard_db_name(scratch)
    try:
        admin_client[scratch]["gate"].insert_one({"n": 1})
        assert scratch in admin_client.list_database_names()
        admin_client.drop_database(scratch)
        assert scratch not in admin_client.list_database_names()
    finally:
        try:
            admin_client.drop_database(scratch)  # идемпотентно, если assert упал до drop
        except Exception:
            pass


def test_production_bootstrap_idempotent_on_live_cluster(db_name) -> None:
    """Повторный прогон ТОЙ ЖЕ production-функции bootstrap_users() поверх
    живого кластера (localhost exception закрыта, root есть) — не падает и
    ничего не меняет: идемпотентность точки начальных прав на проде."""
    local_uri = _uri("TEST_MONGO_ROOT_LOCAL")
    guard_mongo_uri(local_uri)
    parsed = urlparse(_uri("TEST_MONGO_ADMIN_URI"))
    made, note = migrate.bootstrap_users(
        local_uri=local_uri, db_name=db_name, passwords={},
        root_user=unquote(parsed.username or ""), root_pass=unquote(parsed.password or ""),
    )
    assert note == "bootstrap:admin-auth", made
    assert made == [note], f"идемпотентный прогон что-то изменил: {made}"


# ------------- blocker 2 (экспорт): маркеры R2607_* прогона runner'а под auth


# Миграционная фаза стенда (deploy/scripts/r2607_auth_stand.sh) публикует в
# окружение ТОЛЬКО эти несекретные признаки deploy-equivalent прогона: whitelist
# имён закреплён в самом скрипте и сверяется статически
# (tests/test_deploy_artifacts.py::test_r2607_auth_stand_marker_contract), а
# значения проверяются здесь по существу. Без этого «зелёный» прогон мог бы
# означать лишь то, что `migrate up` под dsbot_migration вообще не запускался.
R2607_MARKERS: tuple[str, ...] = (
    "R2607_MIGRATE_UP_RC",
    "R2607_MIGRATE_STATUS_RC",
    "R2607_MIGRATE_UP_APPLIED",
    "R2607_SCHEMA_LATEST",
    "R2607_SCHEMA_VERSION",
    "R2607_BACKFILL_STEP",
    "R2607_BACKFILL_STATUS",
    "R2607_BACKFILL_PENDING",
    "R2607_BACKFILL_DONE",
    "R2607_LEGACY_ID",
    "R2607_LEGACY_REVISION",
    "R2607_PASSWORDLESS_RC",
    "R2607_PASSWORDLESS_DENIED",
)
BACKFILL_MIGRATION_NAME = "guild-settings-revision-backfill"


def _backfill_id() -> int:
    return next(m.id for m in migrate.MIGRATIONS if m.name == BACKFILL_MIGRATION_NAME)


def _marker(name: str) -> str:
    """Значение маркера из eval-блока стенда. Стенд НЕ поднят — skip; стенд
    поднят (TEST_MONGO_DB задан), а маркера нет — FAIL: миграционная фаза —
    обязательная часть живого контракта, и её «потеря» не имеет права выглядеть
    как отсутствие стенда (тот же принцип, что у _uri для URI-экспортов)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if os.environ.get("TEST_MONGO_DB", "").strip():
        pytest.fail(f"стенд поднят (TEST_MONGO_DB задан), но {name} отсутствует: "
                    "миграционная фаза r2607_auth_stand.sh не отдала обязательный "
                    'маркер — переподними стенд: eval "$(deploy/scripts/r2607_auth_stand.sh up)"')
    pytest.skip(f"{name} не задан — подними auth-стенд: "
                'eval "$(deploy/scripts/r2607_auth_stand.sh up)"')


def _marker_int(name: str) -> int:
    raw = _marker(name)
    assert re.fullmatch(r"-?\d+", raw), f"{name}: ожидалось целое число, получено {raw!r}"
    return int(raw)


def test_r2607_every_marker_is_exported() -> None:
    """Все 13 маркеров обязаны дойти из stdout стенда до окружения pytest —
    набор зафиксирован списком выше и whitelist'ом скрипта."""
    for name in R2607_MARKERS:
        assert _marker(name), name


def test_r2607_deploy_equivalent_cli_returned_zero() -> None:
    """`migrate up` и `migrate status` под dsbot_migration-URI (тот же вход,
    что compose-сервис schema-migrate) на живом --auth сервере — rc=0."""
    assert _marker_int("R2607_MIGRATE_UP_RC") == 0
    assert _marker_int("R2607_MIGRATE_STATUS_RC") == 0


def test_r2607_up_applied_the_whole_plan() -> None:
    """На чистой стендовой БД применён ВСЁ план миграций (ни skip-done, ни
    would-apply — серверный контракт, а не ожидание теста)."""
    assert _marker_int("R2607_MIGRATE_UP_APPLIED") == len(migrate.MIGRATIONS)


def test_r2607_backfill_step_done_under_migration_role() -> None:
    """M7 прошёл под миграционной ролью: done, забэкафил >=1 документ,
    legacy-документ получил ровно revision=0 (не None и не 1)."""
    assert _marker("R2607_BACKFILL_STEP") == f"M{_backfill_id()}"
    assert _marker("R2607_BACKFILL_STATUS") == "done"
    assert _marker_int("R2607_BACKFILL_PENDING") >= 1
    assert _marker_int("R2607_BACKFILL_DONE") >= 1
    assert _marker("R2607_LEGACY_ID").startswith("t07legacy_")
    assert _marker_int("R2607_LEGACY_REVISION") == 0


def test_r2607_latest_matches_schema_version() -> None:
    """latest из `status` (schema_versions) == SCHEMA_VERSION, и экспорт
    сверяется с текущим кодом, а не только сам с собой."""
    assert _marker_int("R2607_SCHEMA_VERSION") == schema.SCHEMA_VERSION
    assert _marker_int("R2607_SCHEMA_LATEST") == schema.SCHEMA_VERSION


def test_r2607_passwordless_cli_denied_by_auth() -> None:
    """Безпарольный честный loopback после bootstrap отвергнут rc!=0 именно
    авторизацией: иначе «доказательство auth» было бы ложным (таймаут или
    отсутствие пакета дали бы тот же ненулевой rc)."""
    assert _marker_int("R2607_PASSWORDLESS_RC") != 0
    assert _marker("R2607_PASSWORDLESS_DENIED") == "unauthorized"


def test_r2607_markers_agree_with_the_server(db_name, migration_db) -> None:
    """Сверка маркеров с живым сервером под dsbot_migration: стенд обязан
    рассказывать правду о состоянии БД, а не печатать правдоподобный текст."""
    mid = _backfill_id()
    doc = migration_db[migrate.MIG_COLL].find_one({"_id": mid})
    assert doc is not None, f"schema_migrations не содержит M{mid}"
    assert doc["status"] == _marker("R2607_BACKFILL_STATUS"), doc
    assert int(doc["report"]["backfilled"]) == _marker_int("R2607_BACKFILL_DONE"), doc
    version = migration_db[migrate.VERSION_COLL].find_one({"_id": "schema"})
    assert int(version["version"]) == _marker_int("R2607_SCHEMA_LATEST"), version
    legacy = migration_db[migrate.GUILD_SETTINGS].find_one({"_id": _marker("R2607_LEGACY_ID")})
    assert legacy is not None, "стендовый legacy-документ исчез из guild_settings"
    assert legacy["revision"] == _marker_int("R2607_LEGACY_REVISION"), legacy
