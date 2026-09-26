"""T16 (п.6): retention — конфигурируемый и dry-run-only до решения D07.

Здесь же главный негативный инвариант: в модуле нет ни delete_many, ни
unlink/rmtree — стирать архив нечем, как бы ни был настроен срок.
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from voice_tracker import retention


class _Cursor(list):
    def sort(self, *_a, **_k):
        return self

    def limit(self, _n):
        return self

    def __iter__(self):
        return list.__iter__(self)


class _Chat:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    def count_documents(self, query: dict) -> int:
        boundary = query["deletedAt"]["$lte"]
        return sum(
            1 for d in self.docs
            if isinstance(d.get("deletedAt"), datetime) and d["deletedAt"] <= boundary
        )

    def find(self, query: dict, *_a, **_k) -> _Cursor:
        return _Cursor(
            [d for d in self.docs if isinstance(d.get("deletedAt"), datetime)]
        )


class _Db:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._chat = _Chat(docs)

    def __getitem__(self, _name: str) -> _Chat:
        return self._chat


def _doc(message_id: str, deleted_days_ago: float | None) -> dict[str, Any]:
    deleted = None if deleted_days_ago is None else datetime.now(UTC) - timedelta(days=deleted_days_ago)
    return {"messageId": message_id, "deletedAt": deleted}


def test_days_from_env_parsing() -> None:
    assert retention.deleted_days_from_env({}) == 0
    assert retention.deleted_days_from_env({"RETENTION_DELETED_DAYS": ""}) == 0
    assert retention.deleted_days_from_env({"RETENTION_DELETED_DAYS": "0"}) == 0
    assert retention.deleted_days_from_env({"RETENTION_DELETED_DAYS": "-3"}) == 0
    assert retention.deleted_days_from_env({"RETENTION_DELETED_DAYS": "junk"}) == 0
    assert retention.deleted_days_from_env({"RETENTION_DELETED_DAYS": "365"}) == 365


def test_scan_without_term_reports_unconfigured() -> None:
    out = retention.scan_deleted_messages(_Db([_doc("1", 400)]), days=0)
    assert out["configured"] is False and out["candidates"] is None


def test_scan_counts_only_old_tombstones() -> None:
    db = _Db([_doc("1", 400), _doc("2", 10), _doc("3", None), _doc("4", 366.5)])
    out = retention.scan_deleted_messages(db, days=365)
    assert out["configured"] is True
    assert out["candidates"] == 2  # «3» живая (deletedAt=None), «2» слишком свежая
    assert out["oldestDeletedAt"]


def test_orphan_media_counts_unreferenced_but_tombstone_refs_are_alive(tmp_path: Path) -> None:
    root = tmp_path / "media"
    (root / "170000000000000000" / "2026-09").mkdir(parents=True)
    kept = root / "170000000000000000" / "2026-09" / "a.png"
    kept.write_bytes(b"12345")
    orphan = root / "170000000000000000" / "2026-09" / "orphan.png"
    orphan.write_bytes(b"x")

    class _Db:
        def __getitem__(self, _name):
            class _C:
                def find(self, *_a, **_k):
                    return [
                        # ссылка из tombstone-сообщения — живая (D04/D07)
                        {"attachments": [{"stored": True, "path": "170000000000000000/2026-09/a.png"}]},
                        {"attachments": [{"stored": False, "path": ""}]},
                    ]

            return _C()

    out = retention.scan_orphan_media(_Db(), str(root))
    assert out["referenced"] == 1
    assert out["files"] == 1 and out["bytes"] == 1
    assert out["samples"] == ["170000000000000000/2026-09/orphan.png"]
    assert kept.exists() and orphan.exists()  # dry-run: файлы на месте


def test_module_has_no_deletion_primitive() -> None:
    # plan п.6: dry-run-only. Никаких delete/unlink/remove даже в виде импорта.
    src = inspect.getsource(retention)
    for banned in ("delete_many", "delete_one", "unlink(", "rmtree", "remove("):
        assert banned not in src, f"retention не должен содержать {banned}"
