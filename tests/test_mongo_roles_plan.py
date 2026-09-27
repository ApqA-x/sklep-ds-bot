"""R26-07: финальные юнит-тесты модели прав (DB06) — план и репарация без сети.

Enforcement привилегий реальным сервером доказывает интеграционный стенд
tests/test_mongo_auth_stand.py. Здесь закрывается вторая половина:

  * ROLE_PLAN/USER_PLAN сверяются ДЕКЛАРАТИВНО — регрессионный щит DB06, чтобы
    runtime-роли нельзя было вернуть к прошлому составу (встроенная readWrite
    реально разрешает createIndex/dropIndex/dropCollection, проверено rolesInfo
    на Mongo 7 — ADR-0005);
  * ROOT_ROLE_PLAN — ТОЧНЫЙ состав bootstrap root (review R26-07, blocker 2):
    единый для production bootstrap_users и r2607-стенда, минимально
    достаточный для create/repair ролей и пользователей плана, выдачи им
    built-in backup/restore и штатного restore-cleanup dropDatabase. Здесь он
    закреплён поимённо; эквивалентность боевого пути и стенда на живом сервере
    доказывает tests/test_mongo_auth_stand.py (root там создаётся точной
    production-командой `migrate users --bootstrap` и его роли сверяются с
    сервером);
  * ensure_roles/ensure_users на фейковой БД с журналом команд: съехавший состав
    роли чинится updateRole (а не создаётся заново), leftover-роль пользователя
    отзывается grant/revoke-репарацией, повтор прогона ничего не делает,
    пароли существующих не ротируются;
  * bootstrap_users — обе ветки (localhost exception закрыта / не закрыта) через
    подмену pymongo.MongoClient, без реального сервера.
"""

from __future__ import annotations

import copy
import json
from urllib.parse import quote_plus

import pytest

from voice_tracker import migrate

DB = "voice_tracker_test"

# DB06/R26-07: ни одна runtime-роль не смеет давать ни DDL, ни админских прав.
FORBIDDEN_FOR_RUNTIME: frozenset[str] = frozenset({
    "createIndex", "dropIndex", "dropCollection", "createCollection",
    "userAdmin", "grantRole", "revokeRole", "bypassDocumentValidation",
    "validate", "renameCollectionSameDB", "convertToCapped",
})
# Встроенные роли, которые план не выдаёт НИКОМУ (прошлый состав runtime-юзера).
FORBIDDEN_BUILTINS: frozenset[str] = frozenset({
    "readWrite", "read", "dbAdmin", "admin", "userAdmin", "dbOwner",
    "readWriteAnyDatabase", "dbAdminAnyDatabase", "userAdminAnyDatabase",
    "clusterAdmin",
})
# Единственные встроенные роли плана — все в admin (ADR-0005).
PLAN_BUILTINS: frozenset[str] = frozenset({"backup", "restore", "readAnyDatabase"})
RUNTIME_ROLES = ("dsbot_runtime_bot_role", "dsbot_runtime_web_role")
# Ожидаемые grants: {dbname} — плейсхолдер рабочей БД.
EXPECTED_GRANTS: dict[str, tuple[tuple[str, str], ...]] = {
    "dsbot_app": (("dsbot_runtime_bot_role", "{dbname}"),),
    "dsbot_web": (("dsbot_runtime_web_role", "{dbname}"),),
    "dsbot_migration": (("dsbot_migration_role", "{dbname}"),),
    "dsbot_backup": (("backup", "admin"),),
    "dsbot_restore": (("restore", "admin"), ("readAnyDatabase", "admin")),
}
PLAN_USERS = tuple(EXPECTED_GRANTS)
# Review R26-07 (blocker 2): bootstrap root — ОДИН состав на всех (production
# bootstrap_users и r2607-стенд берут его из migrate.ROOT_ROLE_PLAN). Список
# ниже — независимая фиксация в тесте: «минимально достаточно, но не меньше»:
# userAdminAnyDatabase — createUser/grantRole/createRole; readWriteAnyDatabase —
# CRUD-привилегии, которые обязан владеть грантер кастомных ролей плана;
# dbAdminAnyDatabase — DDL-состав createRole migration-роли + dropDatabase для
# штатного restore-cleanup (restore.sh гейт под MONGO_ADMIN_URI); backup/
# restore — транзитивное владение выдаваемыми built-in ролями dsbot_backup/
# dsbot_restore; clusterMonitor — админ-гейты. Ни admin, ни root superuser.
EXPECTED_ROOT_ROLES: tuple[tuple[str, str], ...] = (
    ("userAdminAnyDatabase", "admin"),
    ("readWriteAnyDatabase", "admin"),
    ("dbAdminAnyDatabase", "admin"),
    ("backup", "admin"),
    ("restore", "admin"),
    ("clusterMonitor", "admin"),
)


# ------------------------------------------------------------- фейковая БД R26-07


class FakeCommandError(Exception):
    """Фейк pymongo OperationFailure: migrate различает ветки по code/code_name."""

    def __init__(self, message: str, code: int | None = None, code_name: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.code_name = code_name


def _already_exists(message: str) -> FakeCommandError:
    return FakeCommandError(message, code=51, code_name="AlreadyExists")


def _unauthorized(message: str) -> FakeCommandError:
    return FakeCommandError(message, code=13, code_name="Unauthorized")


class FakeUsersDB:
    """Мини-сервер управления ролями/пользователями: ровно те команды, которые
    дёргает migrate. Каждый вызов попадает в журнал self.log — тесты сверяют
    ЖУРНАЛ (что реально произошло на сервере), а не только отчёт функции."""

    def __init__(self, name: str = DB) -> None:
        self.name = name
        self.roles: dict[tuple[str, str], dict] = {}
        self.users: dict[str, dict] = {}
        self.log: list[tuple[str, tuple, dict]] = []

    # ------------------------------------------------------------- журнал
    @property
    def ops(self) -> list[str]:
        return [op for op, _args, _kw in self.log]

    def calls(self, op: str) -> list[tuple[tuple, dict]]:
        return [(args, kw) for name, args, kw in self.log if name == op]

    # ------------------------------------------------------- состояние
    def role_actions(self, role: str, db: str | None = None) -> set[str]:
        doc = self.roles[(role, db or self.name)]
        return {a for priv in doc["privileges"] for a in priv["actions"]}

    def user_roles(self, username: str) -> set[tuple[str, str]]:
        return {(r["role"], r["db"]) for r in self.users[username]["roles"]}

    def seed_role(self, role: str, actions: set[str], inherited: list[dict] | None = None) -> None:
        """Роль «как на сервере» — для сценариев дрейфа."""
        self.roles[(role, self.name)] = {
            "role": role, "db": self.name,
            "privileges": [{"resource": {"db": self.name, "collection": ""},
                            "actions": sorted(actions)}],
            "roles": list(inherited or []),
        }

    def seed_user(self, username: str, roles: list[dict], pwd: str = "old-secret") -> None:
        self.users[username] = {"user": username, "db": self.name, "pwd": pwd,
                               "roles": copy.deepcopy(roles)}

    # ------------------------------------------------------- команды
    def command(self, op: str, *args, **kw):
        self.log.append((op, args, kw))
        handler = getattr(self, "_" + op, None)
        if handler is None:
            raise FakeCommandError(f"no such command {op!r}", code=59, code_name="CommandNotFound")
        return handler(*args, **kw)

    def _createRole(self, role, privileges=None, roles=None):
        if (role, self.name) in self.roles:
            raise _already_exists(f"role '{role}' already exists")
        self.roles[(role, self.name)] = {
            "role": role, "db": self.name,
            "privileges": copy.deepcopy(list(privileges or [])),
            "roles": list(roles or []),
        }
        return {"ok": 1}

    def _updateRole(self, role, privileges=None, roles=None):
        if (role, self.name) not in self.roles:
            raise FakeCommandError(f"role '{role}' not found", code=31, code_name="RoleNotFound")
        self.roles[(role, self.name)]["privileges"] = copy.deepcopy(list(privileges or []))
        self.roles[(role, self.name)]["roles"] = list(roles or [])
        return {"ok": 1}

    def _rolesInfo(self, selector=None, showPrivileges=False):
        if selector is None:
            wanted = list(self.roles)
        elif isinstance(selector, str):
            wanted = [(selector, self.name)]
        else:
            wanted = [(d["role"], d.get("db", self.name)) for d in selector]
        return {"roles": [copy.deepcopy(self.roles[k]) for k in wanted if k in self.roles],
                "ok": 1}

    def _usersInfo(self, selector=None):
        if selector is None:
            docs = list(self.users.values())
        else:
            name = selector if isinstance(selector, str) else selector.get("user")
            docs = [self.users[name]] if name in self.users else []
        return {"users": [copy.deepcopy(d) for d in docs], "ok": 1}

    def _createUser(self, username, pwd=None, roles=None):
        if username in self.users:
            raise _already_exists(f"user '{username}' already exists")
        assert pwd, "createUser обязан получить пароль"
        self.users[username] = {"user": username, "db": self.name, "pwd": pwd,
                               "roles": copy.deepcopy(list(roles or []))}
        return {"ok": 1}

    def _grantRolesToUser(self, username, roles=None):
        target = self.users[username]["roles"]
        for role in roles or []:
            if role not in target:
                target.append(copy.deepcopy(role))
        return {"ok": 1}

    def _revokeRolesFromUser(self, username, roles=None):
        target = self.users[username]["roles"]
        for role in roles or []:
            if role in target:
                target.remove(role)
        return {"ok": 1}

    def _updateUser(self, username, **kw):
        # Ротация пароля — явный шаг оператора (ADR-0005), не часть ensure_users.
        raise AssertionError(f"updateUser({username}) вызван: пароли существующих не ротируются")


# ------------------------------------------------- 1. декларативный контракт плана


class TestRolePlanContract:
    """Регрессионный щит DB06: состав привилегий читается прямо из ROLE_PLAN/
    USER_PLAN — если кто-то вернёт DDL в runtime-роли или readWrite пользователю,
    падает здесь, а не на проде."""

    def test_role_plan_has_exactly_three_custom_roles(self) -> None:
        assert set(migrate.ROLE_PLAN) == {
            "dsbot_runtime_bot_role", "dsbot_runtime_web_role", "dsbot_migration_role"}

    @pytest.mark.parametrize("role", RUNTIME_ROLES)
    def test_runtime_roles_carry_no_ddl_or_admin_actions(self, role: str) -> None:
        actions = set(migrate.ROLE_PLAN[role])
        assert not (actions & FORBIDDEN_FOR_RUNTIME), sorted(actions & FORBIDDEN_FOR_RUNTIME)
        # ровно контракт CRUD: ни лишних действий, ни молча добавленного createIndex
        assert actions == set(migrate.RUNTIME_ROLE_ACTIONS)
        assert migrate.ROLE_PLAN[role] == migrate.RUNTIME_ROLE_ACTIONS

    def test_migration_role_is_runtime_crud_plus_ddl_only(self) -> None:
        # ТОЧНЫЙ plan роли runner'а: отличие от runtime — ровно DDL-четвёрка,
        # ничего больше (ни read-действий, ни destructive-удалений коллекций)
        assert migrate.MIGRATION_ROLE_EXTRA == (
            "createIndex", "dropIndex", "createCollection", "collMod")
        assert migrate.ROLE_PLAN["dsbot_migration_role"] == (
            migrate.RUNTIME_ROLE_ACTIONS + migrate.MIGRATION_ROLE_EXTRA)
        actions = set(migrate.ROLE_PLAN["dsbot_migration_role"])
        assert set(migrate.RUNTIME_ROLE_ACTIONS) <= actions  # весь CRUD runtime-набора
        assert {"createIndex", "dropIndex", "createCollection", "collMod"} <= actions
        assert actions == set(migrate.RUNTIME_ROLE_ACTIONS) | set(migrate.MIGRATION_ROLE_EXTRA)

    @pytest.mark.parametrize("role", tuple(migrate.ROLE_PLAN))
    def test_plan_uses_only_real_mongo_authorization_actions(self, role: str) -> None:
        """createRole принимает ТОЛЬКО действия из списка privilege actions Mongo.
        Живой прогон r2607-стенда на mongo:7 дважды это подтвердил:
        `Unrecognized action: getMore`, затем `Unrecognized action: aggregate`
        (BadValue, code 2) — план падал ещё до миграционной фазы. Оба имени тут
        запрещены: только-читающая агрегация авторизуется правом find, а
        continuation курсора — правами исходной операции чтения. Значит, это НЕ
        недостающие гранты, а ошибка плана: «страховка» несуществующим action
        обрывает bootstrap прав целиком, и стенд падает ещё до `migrate up`."""
        assert "aggregate" not in migrate.ROLE_PLAN[role]
        assert "getMore" not in migrate.ROLE_PLAN[role]

    def test_migration_role_has_no_admin_or_destructive_actions(self) -> None:
        actions = set(migrate.ROLE_PLAN["dsbot_migration_role"])
        assert not (actions & (FORBIDDEN_FOR_RUNTIME - {"createIndex", "dropIndex",
                                                       "createCollection", "collMod"}))
        # доступ к данным только в рабочей БД (плейсхолдер в _role_privileges) →
        # ни dropCollection, ни userAdmin/grantRole/revokeRole быть не может
        assert not (actions & {"dropCollection", "userAdmin", "grantRole", "revokeRole"})

    def test_runtime_role_actions_are_data_plane_only(self) -> None:
        assert set(migrate.RUNTIME_ROLE_ACTIONS) == {
            "find", "insert", "update", "remove",
            "listCollections", "listIndexes", "collStats", "dbStats", "killCursors"}
        # getMore — не серверное action (createRole на mongo:7: Unrecognized action);
        # continuation курсора авторизуется правом исходного чтения, поэтому его
        # тут нет (aggregate — см. test_plan_uses_only_real_mongo_authorization_actions)
        assert "getMore" not in migrate.RUNTIME_ROLE_ACTIONS
        assert not (set(migrate.RUNTIME_ROLE_ACTIONS) & FORBIDDEN_FOR_RUNTIME)

    def test_plan_actions_are_unique(self) -> None:
        for role, actions in migrate.ROLE_PLAN.items():
            assert len(actions) == len(set(actions)), role

    @pytest.mark.parametrize("username", PLAN_USERS)
    def test_user_plan_grants_exactly_expected_roles(self, username: str) -> None:
        entry = next(u for u in migrate.USER_PLAN if u[0] == username)
        assert entry[1] == EXPECTED_GRANTS[username]
        assert entry[2].strip(), f"{username}: план обязан объяснять назначение"

    def test_user_plan_is_the_five_service_accounts(self) -> None:
        names = tuple(u for u, _roles, _note in migrate.USER_PLAN)
        assert names == PLAN_USERS  # порядок = порядок создания
        assert len(set(names)) == len(names)

    @pytest.mark.parametrize("username", PLAN_USERS)
    def test_no_plan_user_gets_builtin_privilege_role(self, username: str) -> None:
        _user, roles, _note = next(u for u in migrate.USER_PLAN if u[0] == username)
        granted = {role for role, _db in roles}
        assert not (granted & FORBIDDEN_BUILTINS), sorted(granted & FORBIDDEN_BUILTINS)
        # запрет и комбо прошлого (readWrite+backup), и любой встроенной роли вне
        # списка: допустимы только кастомные роли плана + PLAN_BUILTINS
        assert granted <= (set(migrate.ROLE_PLAN) | PLAN_BUILTINS), granted

    @pytest.mark.parametrize("username", PLAN_USERS)
    def test_every_desired_role_name_resolves(self, username: str) -> None:
        _user, roles, _note = next(u for u in migrate.USER_PLAN if u[0] == username)
        for role, db in roles:
            if role in migrate.ROLE_PLAN:
                assert db == "{dbname}", (username, role, db)  # кастомные — в рабочей БД
            else:
                assert role in PLAN_BUILTINS and db == "admin", (username, role, db)

    def test_desired_roles_expand_placeholder(self) -> None:
        assert migrate._desired_roles("dsbot_app", "other_db") == [
            {"role": "dsbot_runtime_bot_role", "db": "other_db"}]
        assert migrate._desired_roles("dsbot_backup", "other_db") == [
            {"role": "backup", "db": "admin"}]
        with pytest.raises(ValueError, match="вне USER_PLAN"):
            migrate._desired_roles("root", "other_db")

    def test_role_privileges_scope_whole_db_with_sorted_actions(self) -> None:
        for role in migrate.ROLE_PLAN:
            privileges = migrate._role_privileges(role, "voice_tracker_prod")
            assert len(privileges) == 1
            assert privileges[0]["resource"] == {"db": "voice_tracker_prod", "collection": ""}
            assert privileges[0]["actions"] == sorted(migrate.ROLE_PLAN[role])


# ------------------------------------------------- 1b. ROOT_ROLE_PLAN (blocker 2)


class TestRootRolePlan:
    """Точный состав bootstrap root — единый для production `bootstrap_users`
    и r2607-стенда (review R26-07, blocker 2). Декларативная половина: живой
    стенд создаёт root этой же production-командой и test_mongo_auth_stand.py
    сверяет роли root'а УЖЕ НА СЕРВЕРЕ ( authenticatedUserRoles ), т.е.
    эквивалентность «прод == стенд» проверяется фактом, а не комментарием."""

    def test_root_role_plan_exact_composition(self) -> None:
        # порядок и состав — поимённо: любой дрейф (добавили admin/root, убрали
        # backup/restore, перенесли роль из admin) падает здесь, а не на проде
        assert tuple(migrate.ROOT_ROLE_PLAN) == EXPECTED_ROOT_ROLES

    def test_root_roles_doc_is_the_plan_in_server_shape(self) -> None:
        # то, что реально уходит в createUser серверу — ровно план, без потерь
        assert migrate.root_roles_doc() == [{"role": r, "db": d}
                                            for r, d in EXPECTED_ROOT_ROLES]

    def test_root_roles_are_unique_and_admin_scoped(self) -> None:
        names = [r for r, _d in migrate.ROOT_ROLE_PLAN]
        assert len(names) == len(set(names))  # дубль роли в createUser — не «минимально», а баг
        assert {d for _r, d in migrate.ROOT_ROLE_PLAN} == {"admin"}

    def test_root_is_not_superuser(self) -> None:
        # минимальная достаточность: ни встроенного admin, ни root, ни __system
        assert not ({r for r, _d in migrate.ROOT_ROLE_PLAN}
                    & {"admin", "root", "__system"})

    def test_root_owns_every_builtin_role_the_plan_grants(self) -> None:
        # грантер обязан владеть выдаваемыми built-in ролями (иначе createUser/
        # grant dsbot_backup с backup@admin дают Unauthorized на живом mongod —
        # именно из-за этого bootstrap_users с одной userAdminAnyDatabase не
        # проходил план)
        owned = {r for r, _d in migrate.ROOT_ROLE_PLAN}
        granted = {role for _u, roles, _n in migrate.USER_PLAN
                   for role, db in roles if db == "admin"}
        assert granted, "план обязан кому-то выдавать built-in роли"
        # backup/restore несут cluster-специфичные действия (backup/restore на
        # ресурсе cluster): их грантер не может «вывести» из AnyDatabase-ролей —
        # root обязан владеть ими буквально
        assert {"backup", "restore"} <= owned & granted, (sorted(owned), sorted(granted))
        # readAnyDatabase — единственная built-in плана, которой root НЕ владеет
        # по имени: выдача проходит containment'ом привилегий readWriteAnyDatabase
        # (надмножество find по всем БД). Держать эту тонкость «на вере» нельзя —
        # живой прогон createUser dsbot_restore (backup/restore+readAnyDatabase)
        # идёт production-путём на стенде; неверное предположение там же и упадёт.
        assert (granted - {"backup", "restore"}) == {"readAnyDatabase"}
        assert ("readWriteAnyDatabase", "admin") in migrate.ROOT_ROLE_PLAN

    def test_root_plan_covers_bootstrap_and_restore_cleanup_needs(self) -> None:
        need = {"userAdminAnyDatabase",    # createRole/createUser/grant по плану
                "readWriteAnyDatabase",    # CRUD-привилегии грантера createRole
                "dbAdminAnyDatabase",      # DDL-состав createRole + dropDatabase cleanup
                "backup", "restore",       # выдача dsbot_backup/dsbot_restore
                "clusterMonitor"}          # админ-гейты (списки/диагностика)
        assert {r for r, _d in migrate.ROOT_ROLE_PLAN} == need


# ------------------------------------------------------------ 2. ensure_roles


class TestEnsureRoles:
    """Съехавший состав роли — ПЕРЕЗАПИСЫВАЕТСЯ (updateRole), а не принимается
    молча: иначе DB06 держится только на честном слове (R26-07)."""

    def test_fresh_server_creates_every_custom_role_from_plan(self) -> None:
        db = FakeUsersDB()
        made = migrate.ensure_roles(db)
        assert made == [f"role:{r}" for r in migrate.ROLE_PLAN]
        assert [args[0] for args, _kw in db.calls("createRole")] == list(migrate.ROLE_PLAN)
        assert db.calls("updateRole") == []
        for role, actions in migrate.ROLE_PLAN.items():
            doc = db.roles[(role, DB)]
            assert doc["roles"] == []  # без унаследованных ролей (иначе leftover утечёт)
            assert doc["privileges"] == [{"resource": {"db": DB, "collection": ""},
                                         "actions": sorted(actions)}]

    def test_drifted_role_is_repaired_by_update_role_not_recreated(self) -> None:
        db = FakeUsersDB()
        # сервер «съехал»: runtime-роль с лишним createIndex — ровно DB06-нарушение
        db.seed_role("dsbot_runtime_bot_role",
                     set(migrate.ROLE_PLAN["dsbot_runtime_bot_role"]) | {"createIndex"})
        made = migrate.ensure_roles(db)
        assert "role-repaired:dsbot_runtime_bot_role" in made
        assert "role:dsbot_runtime_bot_role" not in made  # не создавалась заново
        assert [args[0] for args, _kw in db.calls("updateRole")] == ["dsbot_runtime_bot_role"]
        # ensure_roles сначала ВСЕГДА пробует createRole для каждой роли плана;
        # на AlreadyExists читает rolesInfo и зовёт updateRole только при дрейфе
        # состава. Поэтому первая попытка создания есть по всем трём именам, а
        # repair-путь отличается ровно одним updateRole и записью role-repaired:
        # вместо role: (см. ассерты выше) — не тем, что createRole не пробовался.
        assert [args[0] for args, _kw in db.calls("createRole")] == list(migrate.ROLE_PLAN) == [
            "dsbot_runtime_bot_role", "dsbot_runtime_web_role", "dsbot_migration_role"]
        assert "createIndex" not in db.role_actions("dsbot_runtime_bot_role")
        # перезапись — ровно планом, одним privilege на (db, "")
        assert db.roles[("dsbot_runtime_bot_role", DB)]["privileges"] == [
            {"resource": {"db": DB, "collection": ""},
             "actions": sorted(migrate.ROLE_PLAN["dsbot_runtime_bot_role"])}]

    def test_role_with_inherited_roles_is_repaired(self) -> None:
        db = FakeUsersDB()
        # actions формально по плану, но роль унаследовала readWrite → привилегии
        # шире плана: состав считается НЕсовпадающим (R26-07)
        db.seed_role("dsbot_runtime_web_role", set(migrate.ROLE_PLAN["dsbot_runtime_web_role"]),
                     inherited=[{"role": "readWrite", "db": DB}])
        made = migrate.ensure_roles(db)
        assert "role-repaired:dsbot_runtime_web_role" in made
        assert db.roles[("dsbot_runtime_web_role", DB)]["roles"] == []

    def test_second_run_touches_nothing(self) -> None:
        db = FakeUsersDB()
        migrate.ensure_roles(db)
        db.log.clear()
        assert migrate.ensure_roles(db) == []
        assert db.calls("updateRole") == []
        # повтор: createRole→already exists→rolesInfo-сверка, никакой записи
        assert db.ops == ["createRole", "rolesInfo"] * len(migrate.ROLE_PLAN)

    def test_unexpected_server_error_propagates(self) -> None:
        db = FakeUsersDB()
        db.command = lambda op, *a, **kw: self._boom(db, op, a, kw)  # type: ignore[method-assign]
        with pytest.raises(FakeCommandError, match="not authorized"):
            migrate.ensure_roles(db)

    @staticmethod
    def _boom(db: FakeUsersDB, op: str, args: tuple, kw: dict):
        db.log.append((op, args, kw))
        if op == "createRole":
            raise _unauthorized("not authorized on " + db.name)
        raise AssertionError(f"ensure_roles не должен дойти до {op}")


# ------------------------------------------------------------ 3. ensure_users


class TestEnsureUsers:
    """Grants-репарация (R26-07): избыточные роли отзываются, недостающие
    выдаются, повторный прогон — no-op; root и прочие вне плана не трогаются."""

    def test_new_users_created_with_desired_roles(self) -> None:
        db = FakeUsersDB()
        pw = {u: f"pw-{u}" for u in PLAN_USERS}
        made = migrate.ensure_users(db, passwords=pw)
        assert [m for m in made if m.startswith("user:")] == [f"user:{u}" for u in PLAN_USERS]
        assert [args[0] for args, _kw in db.calls("createUser")] == list(PLAN_USERS)
        for username in PLAN_USERS:
            assert db.user_roles(username) == {(r["role"], r["db"])
                                               for r in migrate._desired_roles(username, DB)}
        # роли уже в createUser → грантов/реверков после создания нет
        assert db.calls("grantRolesToUser") == [] and db.calls("revokeRolesFromUser") == []
        # отчёт — только имена: секреты наружу не уходят (DB06)
        assert not [m for m in made if "pw-" in m]

    def test_missing_password_skips_user_without_failing_the_run(self) -> None:
        db = FakeUsersDB()
        made = migrate.ensure_users(db, passwords={})
        assert db.calls("createUser") == []
        assert [m for m in made if m.startswith("user:")] == []
        assert [m for m in made if m.startswith("role:")] == [f"role:{r}" for r in migrate.ROLE_PLAN]

    def test_leftover_readwrite_revoked_then_plan_role_granted(self) -> None:
        db = FakeUsersDB()
        db.seed_user("dsbot_app", [{"role": "readWrite", "db": DB}])
        made = migrate.ensure_users(db, passwords={"dsbot_app": "pw-app"})
        assert "revoke:dsbot_app:readWrite" in made
        assert "grant:dsbot_app:dsbot_runtime_bot_role" in made
        # журнал: ОТЗЫВ был и ГРАНТ был, отзыв раньше гранта; имя юзера —
        # первый позиционный аргумент серверной команды
        assert db.calls("revokeRolesFromUser") and db.calls("grantRolesToUser")
        assert db.ops.index("revokeRolesFromUser") < db.ops.index("grantRolesToUser")
        assert [("dsbot_app", [{"role": "readWrite", "db": DB}])] == [
            (args[0], kw["roles"]) for args, kw in db.calls("revokeRolesFromUser")]
        assert db.user_roles("dsbot_app") == {("dsbot_runtime_bot_role", DB)}
        # второй прогон: ни реверка, ни гранта, ни повторного создания
        db.log.clear()
        again = migrate.ensure_users(db, passwords={"dsbot_app": "pw-app"})
        assert [m for m in again if m.startswith(("user:", "grant:", "revoke:", "role-repaired"))] == []
        assert db.calls("grantRolesToUser") == [] and db.calls("revokeRolesFromUser") == []
        assert db.calls("createUser") == []

    def test_revoke_only_when_plan_role_already_granted(self) -> None:
        db = FakeUsersDB()
        db.seed_user("dsbot_app", [{"role": "dsbot_runtime_bot_role", "db": DB},
                                   {"role": "readWrite", "db": DB}])
        made = migrate.ensure_users(db, passwords={})
        assert "revoke:dsbot_app:readWrite" in made
        assert [m for m in made if m.startswith("grant:")] == []
        assert db.calls("grantRolesToUser") == []
        assert db.user_roles("dsbot_app") == {("dsbot_runtime_bot_role", DB)}

    def test_user_exactly_per_plan_gets_no_grant_no_revoke(self) -> None:
        db = FakeUsersDB()
        for username in PLAN_USERS:
            db.seed_user(username, migrate._desired_roles(username, DB))
        made = migrate.ensure_users(db, passwords={u: "pw-new" for u in PLAN_USERS})
        assert [m for m in made if m.startswith(("user:", "grant:", "revoke:"))] == []
        assert db.calls("grantRolesToUser") == [] and db.calls("revokeRolesFromUser") == []
        assert db.calls("createUser") == []
        for username in PLAN_USERS:
            assert db.user_roles(username) == {(r["role"], r["db"])
                                              for r in migrate._desired_roles(username, DB)}

    def test_existing_passwords_are_not_rotated(self) -> None:
        db = FakeUsersDB()
        for username in PLAN_USERS:
            db.seed_user(username, migrate._desired_roles(username, DB), pwd=f"old-{username}")
        migrate.ensure_users(db, passwords={u: f"new-{u}" for u in PLAN_USERS})
        assert "updateUser" not in db.ops  # фейк бросает AssertionError на таком вызове
        for username in PLAN_USERS:
            assert db.users[username]["pwd"] == f"old-{username}"

    def test_accounts_outside_plan_are_untouched(self) -> None:
        """Локаут-безопасность: реверк идёт поимённо по USER_PLAN, root/admin —
        никогда (иначе `migrate users` мог бы снять права с администратора)."""
        db = FakeUsersDB()
        db.seed_user("root", [{"role": "userAdminAnyDatabase", "db": "admin"}])
        db.seed_user("ops_backup", [{"role": "backup", "db": "admin"},
                                    {"role": "readWrite", "db": DB}])
        made = migrate.ensure_users(db, passwords={})
        assert not [m for m in made if m.split(":")[1] in ("root", "ops_backup")]
        for args, _kw in db.calls("revokeRolesFromUser") + db.calls("grantRolesToUser"):
            assert args[0] in PLAN_USERS  # имя юзера — первый позиционный аргумент
        assert db.user_roles("root") == {("userAdminAnyDatabase", "admin")}
        assert ("readWrite", DB) in db.user_roles("ops_backup")  # чужих не понижаем

    def test_backup_and_restore_keep_admin_scoped_builtin_roles(self) -> None:
        db = FakeUsersDB()
        db.seed_user("dsbot_backup", [{"role": "backup", "db": "admin"}])
        db.seed_user("dsbot_restore", [{"role": "restore", "db": "admin"}])
        made = migrate.ensure_users(db, passwords={})
        assert "grant:dsbot_restore:readAnyDatabase" in made  # гейты restore.sh (R26-07)
        assert [m for m in made if m.startswith("revoke:")] == []
        assert db.user_roles("dsbot_backup") == {("backup", "admin")}
        assert db.user_roles("dsbot_restore") == {("restore", "admin"),
                                                 ("readAnyDatabase", "admin")}

    def test_create_user_race_with_existing_user_is_idempotent(self) -> None:
        """Гонка: usersInfo показал отсутствие, createUser ответил already exists →
        это не ошибка, состав ролей сверяется как для существующего."""
        db = FakeUsersDB()
        real_create = db._createUser

        def racing_create(username, pwd=None, roles=None):
            if username == "dsbot_web":
                db.seed_user(username, [{"role": "readWrite", "db": DB}])
                raise _already_exists(f"user '{username}' already exists")
            return real_create(username, pwd=pwd, roles=roles)

        db._createUser = racing_create  # type: ignore[method-assign]
        made = migrate.ensure_users(db, passwords={u: f"pw-{u}" for u in PLAN_USERS})
        # already-exists — не ошибка и не «наше создание»; have берётся из
        # снимка usersInfo ДО цикла, поэтому реверк leftover — только со второго
        # прогона (сверка всё равно сходится, локаута нет)
        assert "user:dsbot_web" not in made
        assert "grant:dsbot_web:dsbot_runtime_web_role" in made
        assert db.user_roles("dsbot_web") == {("readWrite", DB), ("dsbot_runtime_web_role", DB)}
        again = migrate.ensure_users(db, passwords={})
        assert "revoke:dsbot_web:readWrite" in again
        assert db.user_roles("dsbot_web") == {("dsbot_runtime_web_role", DB)}


# ---------------------------------------------------------- 4. bootstrap_users


class FakeAdminDB:
    """`.admin` фейкового клиента: журнал команд + опциональный постоянный отказ."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._fail = fail

    def command(self, op: str, *args, **kw):
        self.calls.append((op, args, kw))
        if self._fail is not None:
            raise self._fail
        return {"ok": 1}


class FakeBootstrapClient:
    def __init__(self, uri: str, admin: FakeAdminDB, db: FakeUsersDB) -> None:
        self.uri = uri
        self.admin = admin
        self._db = db
        self.closed = False

    def __getitem__(self, name: str) -> FakeUsersDB:
        assert name == self._db.name, f"запрошена чужая БД {name!r}"
        return self._db

    def close(self) -> None:
        self.closed = True


class FakeMongoClient:
    """Замена pymongo.MongoClient (bootstrap_users импортирует pymongo внутри
    функции — патчим атрибут модуля). Первый клиент — localhost-exception-сессия,
    ему можно предписать отказ, чтобы пройти ветку admin-auth."""

    def __init__(self, db: FakeUsersDB, first_admin_error: Exception | None = None) -> None:
        self.db = db
        self.first_admin_error = first_admin_error
        self.clients: list[FakeBootstrapClient] = []

    def __call__(self, uri: str, **kw) -> FakeBootstrapClient:
        assert kw.get("serverSelectionTimeoutMS") == 5000
        admin = FakeAdminDB(self.first_admin_error if not self.clients else None)
        client = FakeBootstrapClient(uri, admin, self.db)
        self.clients.append(client)
        return client

    @property
    def uris(self) -> list[str]:
        return [c.uri for c in self.clients]


LOCAL_URI = "mongodb://127.0.0.1:27017"
ROOT_USER, ROOT_PASS = "dsbot_root", "R0t#pw"
PLAN_PW = {u: f"pw-{u}" for u in PLAN_USERS}


class TestBootstrapUsers:
    """`migrate users --bootstrap`: первая учётка через localhost exception,
    при закрытой exception — идемпотентный переход на admin-URI (R26-07 шаг 1)."""

    def test_localhost_exception_creates_root_then_plan(self, monkeypatch) -> None:
        pytest.importorskip("pymongo")
        import pymongo

        db = FakeUsersDB()
        fake = FakeMongoClient(db)
        monkeypatch.setattr(pymongo, "MongoClient", fake)

        made, note = migrate.bootstrap_users(
            local_uri=LOCAL_URI, db_name=DB, passwords=PLAN_PW,
            root_user=ROOT_USER, root_pass=ROOT_PASS)

        assert note == "bootstrap:root-created"
        assert made[0] == note
        # localhost-сессия: ровно один createUser root'а СОСТАВОМ ИЗ ROOT_ROLE_PLAN
        # (review R26-07, blocker 2: одна userAdminAnyDatabase не покрывает ни
        # createRole ролей плана, ни выдачу backup/restore, ни dropDatabase
        # штатного restore-cleanup)
        first = fake.clients[0].admin
        assert [c[0] for c in first.calls] == ["createUser"]
        _op, args, kw = first.calls[0]
        assert args == (ROOT_USER,)
        assert kw["roles"] == [{"role": r, "db": "admin"} for r, _d in EXPECTED_ROOT_ROLES]
        assert fake.uris[0] == LOCAL_URI  # без credentials — и есть localhost exception
        # дальше тот же путь, что у `migrate users`: URI из root-учётки, роли+план
        assert len(fake.clients) == 2
        assert fake.uris[1] == (f"mongodb://{quote_plus(ROOT_USER)}:{quote_plus(ROOT_PASS)}"
                                f"@127.0.0.1:27017/admin?authSource=admin")
        assert "role:dsbot_runtime_bot_role" in made
        assert [m for m in made if m.startswith("user:")] == [f"user:{u}" for u in PLAN_USERS]
        assert db.role_actions("dsbot_runtime_bot_role") <= set(migrate.RUNTIME_ROLE_ACTIONS)
        assert all(c.closed for c in fake.clients)
        assert ROOT_PASS not in json.dumps(made)

    def test_closed_localhost_exception_falls_back_to_admin_auth(self, monkeypatch) -> None:
        pytest.importorskip("pymongo")
        import pymongo

        db = FakeUsersDB()
        # кластер уже под паролем: роли на месте, пользователи — нет
        for role, actions in migrate.ROLE_PLAN.items():
            db.seed_role(role, set(actions))
        fake = FakeMongoClient(db, first_admin_error=_unauthorized(
            "command createUser requires authentication"))
        monkeypatch.setattr(pymongo, "MongoClient", fake)

        made, note = migrate.bootstrap_users(
            local_uri=LOCAL_URI + "/?directConnection=true", db_name=DB, passwords=PLAN_PW,
            root_user=ROOT_USER, root_pass=ROOT_PASS,
            admin_uri="mongodb://dsbot_root:pw@mongo-host:27017/admin")

        assert note == "bootstrap:admin-auth"  # не падает: ветка идемпотентна
        assert made[0] == note
        assert fake.clients[0].admin.calls[0][0] == "createUser"  # попытка была
        assert fake.uris[1] == "mongodb://dsbot_root:pw@mongo-host:27017/admin"  # явный admin_uri
        # ensure_users под admin-сессией: роли не пересоздаются, план создаётся
        assert [m for m in made if m.startswith("role:")] == []
        assert [m for m in made if m.startswith("user:")] == [f"user:{u}" for u in PLAN_USERS]
        assert not any("pw-" in m or ROOT_PASS in m for m in made)
        assert all(c.closed for c in fake.clients)

    def test_non_auth_error_is_not_swallowed(self, monkeypatch) -> None:
        pytest.importorskip("pymongo")
        import pymongo

        db = FakeUsersDB()
        fake = FakeMongoClient(db, first_admin_error=FakeCommandError(
            "connection refused", code=None, code_name="NetworkTimeout"))
        monkeypatch.setattr(pymongo, "MongoClient", fake)
        with pytest.raises(FakeCommandError, match="connection refused"):
            migrate.bootstrap_users(local_uri=LOCAL_URI, db_name=DB, passwords=PLAN_PW,
                                    root_user=ROOT_USER, root_pass=ROOT_PASS)
        # не «unauthorized/already exists» → не глотаем и на admin-ветку не идём:
        # был ровно один (localhost-)клиент, он закрыт в finally
        assert len(fake.clients) == 1
        assert fake.clients[0].closed
        assert fake.uris == [LOCAL_URI]
