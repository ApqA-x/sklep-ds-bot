"""R26-07 review (DB03, blocker 1): startup рантайма — verify-only.

Ни одного DDL- И ни одного write-вызова. Фейк-БД ниже падает на ЛЮБОЙ попытке
изменить схему (createIndex/createIndexes/dropIndex/dropIndexes/drop) или данные
(insert_*/update_*/replace_*/delete_*/bulk_write), поэтому тесты проходят только
если Repository.verify_startup() действительно ограничивается read-only сверкой
schema.verify_db(). CRUD-backfill revision (T06) со startup убран: это была
скрытая миграция мимо runner'а, теперь тот же шаг — миграция M7
(`guild-settings-revision-backfill`) в `python -m voice_tracker.migrate up`.
ensure_indexes() на том же фейке обязан падать — это контрастный тест строгости
фейка, а не «ещё один способ поднять схему».
"""

from __future__ import annotations

import pytest

from voice_tracker import schema
from voice_tracker.repository import Repository

_DDL = "DDL on runtime startup forbidden"
_WRITE = "write on runtime startup forbidden"


class _DDLGuardMixin:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ddl_calls: list[str] = []
        self.write_calls: list[str] = []

    def _forbid(self, bucket: list[str], op: str, message: str):
        bucket.append(op)
        raise AssertionError(message)

    # ---- DDL -------------------------------------------------------------
    def create_index(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.ddl_calls, "create_index", _DDL)

    def create_indexes(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.ddl_calls, "create_indexes", _DDL)

    def drop_index(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.ddl_calls, "drop_index", _DDL)

    def drop_indexes(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.ddl_calls, "drop_indexes", _DDL)

    def drop(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.ddl_calls, "drop", _DDL)

    # ---- записи в данные ---------------------------------------------------
    def insert_one(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.write_calls, "insert_one", _WRITE)

    def insert_many(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.write_calls, "insert_many", _WRITE)

    def update_one(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.write_calls, "update_one", _WRITE)

    def update_many(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.write_calls, "update_many", _WRITE)

    def replace_one(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.write_calls, "replace_one", _WRITE)

    def delete_one(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.write_calls, "delete_one", _WRITE)

    def delete_many(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.write_calls, "delete_many", _WRITE)

    def bulk_write(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid(self.write_calls, "bulk_write", _WRITE)


class _NoVerifyCollection(_DDLGuardMixin):
    """Без list_indexes → None-путь schema._list_indexes → сверка пропущена."""


class _IndexedCollection(_DDLGuardMixin):
    def __init__(self, name: str, docs: list[dict]) -> None:
        super().__init__(name)
        self._docs = docs

    def list_indexes(self):
        return list(self._docs)


class _DDLForbiddenDb:
    def __init__(self) -> None:
        self.cols: dict[str, _DDLGuardMixin] = {}

    def __getitem__(self, name: str) -> _DDLGuardMixin:
        if name not in self.cols:
            self.cols[name] = self._make(name)
        return self.cols[name]

    def _make(self, name: str) -> _DDLGuardMixin:
        return _NoVerifyCollection(name)

    @property
    def ddl_calls(self) -> dict[str, list[str]]:
        return {name: col.ddl_calls for name, col in self.cols.items() if col.ddl_calls}

    @property
    def write_calls(self) -> dict[str, list[str]]:
        return {name: col.write_calls for name, col in self.cols.items() if col.write_calls}


class _IndexedDb(_DDLForbiddenDb):
    """БД с фактическими индексами: [] означает «индекс не создан» → missing."""

    def __init__(self, index_docs: dict[str, list[dict]] | None = None) -> None:
        super().__init__()
        self._index_docs = index_docs or {}

    def _make(self, name: str) -> _DDLGuardMixin:
        return _IndexedCollection(name, self._index_docs.get(name, []))


def _spec_as_actual_index(spec: schema.IndexSpec) -> dict:
    """Индекс, полностью соответствующий спецификации (именно так его видит сверка)."""
    doc = {"key": {field: direction for field, direction in spec.keys}}
    doc.update(spec.create_kwargs())
    return doc


def _assert_no_mutations(db: _DDLForbiddenDb) -> None:
    """Строго: verify_startup не обязан сделать ни одного вызова ни к DDL, ни к
    записи (guard в прокси-коллекциях пишет вызов в bucket перед падением,
    Repository.__init__ заранее берёт ссылки на коллекции — падают именно
    вызовы). Единственный write прошлого — backfill T06 в guild_settings."""
    assert db.ddl_calls == {}
    assert db.write_calls == {}
    gs = db.cols.get("guild_settings")
    if gs is not None:  # ссылку создаёт __init__ — проверяем именно отсутствие вызовов
        assert gs.ddl_calls == [] and gs.write_calls == [], "verify_startup мутировал guild_settings"


def test_verify_startup_passes_and_makes_no_ddl_calls() -> None:
    db = _DDLForbiddenDb()

    Repository(db).verify_startup()

    _assert_no_mutations(db)


def test_verify_startup_makes_no_writes_at_all() -> None:
    """R26-07 review (blocker 1): единственное допустимое действие startup —
    read-only сверка. Нулевые записи проверяются и guard'ом (исключение при
    попытке), и по факту обращений к коллекциям."""
    for db in (_DDLForbiddenDb(), _IndexedDb()):
        Repository(db).verify_startup()
        _assert_no_mutations(db)


def test_write_guard_actually_catches_updates() -> None:
    """Контраст строгости фейка: guard ловит не только DDL, но и записи —
    иначе «нулевые writes» ничего не значат (тот же путь, что был у T06)."""
    db = _DDLForbiddenDb()
    with pytest.raises(AssertionError, match=_WRITE):
        db["guild_settings"].update_many({"revision": {"$exists": False}}, {"$set": {"revision": 0}})
    with pytest.raises(AssertionError, match=_WRITE):
        db["voice_sessions"].insert_one({"n": 1})
    assert db.write_calls == {"guild_settings": ["update_many"], "voice_sessions": ["insert_one"]}


def test_verify_startup_accepts_a_fully_provisioned_db() -> None:
    specs = schema.specs_for(("bot", "shared"))
    docs: dict[str, list[dict]] = {}
    for spec in specs:
        docs.setdefault(spec.collection, []).append(_spec_as_actual_index(spec))
    db = _IndexedDb(docs)

    Repository(db).verify_startup()

    _assert_no_mutations(db)


def test_verify_startup_raises_on_incompatible_index_without_ddl() -> None:
    """Несовместимость не скрывается и не «чинится» пересозданием индекса на startup."""
    spec = schema.specs_for(("bot", "shared"))[0]
    broken = _spec_as_actual_index(spec)
    # те же ключи, противоположный флаг unique → именно incompatible, не missing
    broken["unique"] = not spec.unique
    db = _IndexedDb({spec.collection: [broken]})

    with pytest.raises(schema.SchemaIncompatible):
        Repository(db).verify_startup()

    _assert_no_mutations(db)


def test_verify_startup_does_not_hide_missing_indexes_with_ddl() -> None:
    """Пустая БД: missing не поднимается на startup (это работа migrate up), но и
    никаких мутаций (ни DDL, ни записей) startup не делает."""
    db = _IndexedDb()

    Repository(db).verify_startup()

    _assert_no_mutations(db)


def test_ensure_indexes_is_caught_by_the_same_ddl_guard() -> None:
    """Контраст: bootstrap-путь на этом фейке падает — иначе фейк ничего не проверяет."""
    db = _DDLForbiddenDb()

    with pytest.raises(AssertionError, match=_DDL):
        Repository(db).ensure_indexes(None)
