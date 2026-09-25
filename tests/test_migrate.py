"""T10 unit: логика migration runner без сети (plan, rollback-preflight, dedup-классификация)."""

from __future__ import annotations

import json

import pytest

from voice_tracker import migrate, schema


class FakeCol:
    def __init__(self, name: str, docs: list[dict] | None = None) -> None:
        self.name = name
        self.docs: list[dict] = docs if docs is not None else []
        self.indexes: list[tuple] = []
        self.deleted: list[dict] = []

    def create_index(self, keys, **kw) -> str:
        self.indexes.append((tuple(keys), tuple(sorted(kw.items(), key=lambda kv: str(kv[0])))))
        return kw.get("name") or ""

    def find(self, flt: dict, _proj=None):
        out = []
        for d in self.docs:
            ok = True
            for k, v in flt.items():
                if isinstance(v, dict) and "$in" in v:
                    ok = ok and d.get(k) in v["$in"]
                else:
                    ok = ok and d.get(k) == v
            if ok:
                out.append(d)
        return out

    def find_one(self, flt: dict):
        if "_id" in flt and not isinstance(flt["_id"], dict):
            return next((d for d in self.docs if d["_id"] == flt["_id"]), None)
        found = self.find({k: v for k, v in flt.items() if not isinstance(v, dict)})
        return found[0] if found else None

    def delete_many(self, flt: dict):
        ids = flt.get("_id", {})
        keep = []
        removed = 0
        for d in self.docs:
            if isinstance(ids, dict) and d["_id"] in ids.get("$in", []) and all(
                    d.get(k) == v for k, v in flt.items() if k != "_id"):
                removed += 1
                self.deleted.append(d)
            else:
                keep.append(d)
        self.docs = keep
        return type("R", (), {"deleted_count": removed})()

    def aggregate(self, _pipeline):
        groups: dict[tuple, list] = {}
        for d in self.docs:
            if "guildId" not in d or "entryId" not in d:
                continue
            groups.setdefault((d["guildId"], d["entryId"]), []).append(d["_id"])
        return [{"_id": {"guildId": k[0], "entryId": k[1]}, "ids": v, "n": len(v)}
                for k, v in groups.items() if len(v) > 1][:500]


class FakeDB:
    def __init__(self) -> None:
        self.cols: dict[str, FakeCol] = {}
        self.name = "voice_tracker_test"

    def __getitem__(self, name: str) -> FakeCol:
        return self.cols.setdefault(name, FakeCol(name))


# ----------------------------------------------------------------------- plan


def test_plan_reports_would_apply_for_all_pending_migrations() -> None:
    db = FakeDB()
    result = migrate.plan_and_apply(db, apply=False)
    assert result["dryRun"] is True
    assert [a["action"] for a in result["actions"]] == ["would-apply"] * len(migrate.MIGRATIONS)
    assert [a["id"] for a in result["actions"]] == [1, 2, 3, 4]
    # dry-run ничего не создал
    assert db.cols.get(schema.OP) is None or db.cols[schema.OP].indexes == []


def test_plan_output_json_serializable() -> None:
    json.dumps(migrate.plan_and_apply(FakeDB(), apply=False))


# ------------------------------------------------------------- DB07 rollback


def test_rollback_blocked_over_backward_incompatible_migration() -> None:
    db = FakeDB()
    db[migrate.MIG_COLL].docs = [
        {"_id": 1, "status": "done"},
        {"_id": 2, "status": "done"},
        {"_id": 3, "status": "done", "name": migrate.MIGRATIONS[2].name},
    ]
    # откат на 2 означает «M3 применена, код её не ждёт» → заблокирован
    problems = migrate.check_rollback(db, 2)
    assert any("M3" in p and "backward-incompatible" in p for p in problems)
    # откат на 3 — нечего откатывать
    assert migrate.check_rollback(db, 3) == []


def test_rollback_blocked_while_migration_unfinished() -> None:
    db = FakeDB()
    db[migrate.MIG_COLL].docs = [{"_id": 3, "status": "running"}]
    problems = migrate.check_rollback(db, 2)
    assert any("незавершённая" in p for p in problems)


# ----------------------------------------------------------------- DB05 dedup


def _audit(guild: str, entry: str, doc: dict) -> dict:
    return {"_id": doc["id"], "guildId": guild, "entryId": entry, **{k: v for k, v in doc.items() if k != "id"}}


def test_dup_groups_classifies_identical_vs_conflicting() -> None:
    db = FakeDB()
    da = db[schema.DA]
    da.docs = [
        _audit("g", "e1", {"id": "a", "actionType": "x"}),
        _audit("g", "e1", {"id": "b", "actionType": "x"}),  # доказуемо эквивалентны
        _audit("g", "e2", {"id": "c", "actionType": "x"}),
        _audit("g", "e2", {"id": "d", "actionType": "y"}),  # конфликт
    ]
    dups = migrate._dup_groups(db, schema.DA, ["guildId", "entryId"])
    assert dups["duplicateGroups"] == 2
    assert dups["mergeable"] == 1 and dups["conflicting"] == 1


def test_m3_refuses_to_delete_conflicting_duplicates() -> None:
    db = FakeDB()
    da = db[schema.DA]
    da.docs = [
        _audit("g", "e2", {"id": "c", "actionType": "x"}),
        _audit("g", "e2", {"id": "d", "actionType": "y"}),
    ]
    with pytest.raises(RuntimeError, match="не строится"):
        migrate._apply_discord_audit_unique(db, dry=False)
    assert len(da.docs) == 2  # ничего не удалено (DB05)
    assert da.indexes == []  # unique-индекс не построен


def test_m3_merges_only_identical_duplicates_then_builds_unique() -> None:
    db = FakeDB()
    da = db[schema.DA]
    da.docs = [
        _audit("g", "e1", {"id": "a", "actionType": "x"}),
        _audit("g", "e1", {"id": "b", "actionType": "x"}),
    ]
    report = migrate._apply_discord_audit_unique(db, dry=False)
    assert report["mergedDeleted"] == 1
    assert [d["_id"] for d in da.docs] == ["a"]  # keep=min по str(_id)
    assert any("unique" in str(kw) for _, kw in da.indexes)
