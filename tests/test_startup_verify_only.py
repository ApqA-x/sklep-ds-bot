"""R26-07 (DB03): startup рантайма — verify-only, ни одного DDL-вызова.

Фейк-БД ниже падает на createIndex/createIndexes/dropIndex/dropIndexes/drop,
поэтому тесты проходят только если Repository.verify_startup() действительно
ограничивается CRUD-backfill revision (T06) и read-only сверкой
schema.verify_db(). ensure_indexes() на том же фейке обязан падать — это
контрастный тест строгости фейка, а не «ещё один способ поднять схему».
"""

from __future__ import annotations

import pytest

from voice_tracker import schema
from voice_tracker.repository import Repository

_DDL = "DDL on runtime startup forbidden"


class _DDLGuardMixin:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ddl_calls: list[str] = []
        self.update_many_calls: list[tuple] = []

    def _forbid(self, op: str):
        self.ddl_calls.append(op)
        raise AssertionError(_DDL)

    def create_index(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid("create_index")

    def create_indexes(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid("create_indexes")

    def drop_index(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid("drop_index")

    def drop_indexes(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid("drop_indexes")

    def drop(self, *_args, **_kwargs):  # noqa: ANN002 - fake
        return self._forbid("drop")

    def update_many(self, flt, update, **kwargs):  # noqa: ANN001 - fake
        self.update_many_calls.append((flt, update, kwargs))
        return None


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


def test_verify_startup_passes_and_makes_no_ddl_calls() -> None:
    db = _DDLForbiddenDb()

    Repository(db).verify_startup()

    assert db.ddl_calls == {}


def test_verify_startup_runs_only_the_crud_revision_backfill() -> None:
    db = _DDLForbiddenDb()

    Repository(db).verify_startup()

    # тот же запрос, что в ensure_indexes (T06), и он единственный на startup
    assert db["guild_settings"].update_many_calls == [
        ({"revision": {"$exists": False}}, {"$set": {"revision": 0}}, {})
    ]
    others = {name: col.update_many_calls for name, col in db.cols.items() if name != "guild_settings"}
    assert all(calls == [] for calls in others.values()), others


def test_verify_startup_accepts_a_fully_provisioned_db() -> None:
    specs = schema.specs_for(("bot", "shared"))
    docs: dict[str, list[dict]] = {}
    for spec in specs:
        docs.setdefault(spec.collection, []).append(_spec_as_actual_index(spec))
    db = _IndexedDb(docs)

    Repository(db).verify_startup()

    assert db.ddl_calls == {}


def test_verify_startup_raises_on_incompatible_index_without_ddl() -> None:
    """Несовместимость не скрывается и не «чинится» пересозданием индекса на startup."""
    spec = schema.specs_for(("bot", "shared"))[0]
    broken = _spec_as_actual_index(spec)
    # те же ключи, противоположный флаг unique → именно incompatible, не missing
    broken["unique"] = not spec.unique
    db = _IndexedDb({spec.collection: [broken]})

    with pytest.raises(schema.SchemaIncompatible):
        Repository(db).verify_startup()

    assert db.ddl_calls == {}


def test_verify_startup_does_not_hide_missing_indexes_with_ddl() -> None:
    """Пустая БД: missing не поднимается на startup (это работа migrate up), но и DDL нет."""
    db = _IndexedDb()

    Repository(db).verify_startup()

    assert db.ddl_calls == {}


def test_ensure_indexes_is_caught_by_the_same_ddl_guard() -> None:
    """Контраст: bootstrap-путь на этом фейке падает — иначе фейк ничего не проверяет."""
    db = _DDLForbiddenDb()

    with pytest.raises(AssertionError, match=_DDL):
        Repository(db).ensure_indexes(None)
