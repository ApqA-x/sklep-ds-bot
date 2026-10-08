"""T10 unit: логика migration runner без сети (plan, rollback-preflight,
dedup-классификация, M7 revision-backfill)."""

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
        self.updated: list[tuple] = []

    def create_index(self, keys, **kw) -> str:
        self.indexes.append((tuple(keys), tuple(sorted(kw.items(), key=lambda kv: str(kv[0])))))
        return kw.get("name") or ""

    @staticmethod
    def _match_doc(d: dict, flt: dict) -> bool:
        for k, v in flt.items():
            if isinstance(v, dict):
                if "$exists" in v and bool(k in d) != bool(v["$exists"]):
                    return False
                if "$in" in v and d.get(k) not in v["$in"]:
                    return False
                if not any(m in v for m in ("$exists", "$in")):
                    return False
            elif d.get(k) != v:
                return False
        return True

    def find(self, flt: dict, _proj=None):
        return [d for d in self.docs if self._match_doc(d, flt)]

    def count_documents(self, flt: dict) -> int:
        return sum(1 for d in self.docs if self._match_doc(d, flt))

    def update_many(self, flt: dict, update: dict, **_kwargs):
        modified = 0
        for d in self.docs:
            if not self._match_doc(d, flt):
                continue
            changed = False
            for k, v in (update.get("$set") or {}).items():
                if d.get(k) != v:
                    d[k] = v
                    changed = True
            if changed:
                modified += 1
                self.updated.append((dict(flt), dict(update), d["_id"]))
        return type("R", (), {"modified_count": modified})()

    def find_one(self, flt: dict):
        if "_id" in flt and not isinstance(flt["_id"], dict):
            return next((d for d in self.docs if d["_id"] == flt["_id"]), None)
        found = self.find(flt)
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

    def list_collection_names(self) -> list[str]:
        return list(self.cols)

    def create_collection(self, name: str) -> FakeCol:
        assert name not in self.cols
        return self[name]


# ----------------------------------------------------------------------- plan


def test_plan_reports_would_apply_for_all_pending_migrations() -> None:
    db = FakeDB()
    result = migrate.plan_and_apply(db, apply=False)
    assert result["dryRun"] is True
    assert [a["action"] for a in result["actions"]] == ["would-apply"] * len(migrate.MIGRATIONS)
    assert [a["id"] for a in result["actions"]] == [1, 2, 3, 4, 5, 6, 7, 8]
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


# ------------------------------------------------------- M7 revision-backfill


def test_m7_backfills_only_docs_without_revision() -> None:
    """R26-07 review (blocker 1): backfill переехал со startup рантайма в runner.
    Существующие значения revision не трогаются (фильтр $exists:false)."""
    db = FakeDB()
    gs = db[migrate.GUILD_SETTINGS]
    gs.docs = [{"_id": "g1", "guildId": "1"}, {"_id": "g2", "guildId": "2", "revision": 7}]
    report = migrate._apply_guild_settings_revision_backfill(db, dry=False)
    assert gs.find_one({"_id": "g1"})["revision"] == 0
    assert gs.find_one({"_id": "g2"})["revision"] == 7  # существующее не перезаписано
    assert report["backfilled"] == 1 and report["pending"] == 1 and report["dryRun"] is False


def test_m7_is_idempotent_and_dry_run_writes_nothing() -> None:
    db = FakeDB()
    gs = db[migrate.GUILD_SETTINGS]
    gs.docs = [{"_id": "g1", "guildId": "1"}]

    dry = migrate._apply_guild_settings_revision_backfill(db, dry=True)
    assert dry["dryRun"] is True and dry["pending"] == 1
    assert "revision" not in gs.docs[0] and gs.updated == []  # dry-run — ноль записей

    first = migrate._apply_guild_settings_revision_backfill(db, dry=False)
    assert first["backfilled"] == 1
    again = migrate._apply_guild_settings_revision_backfill(db, dry=False)
    assert again["backfilled"] == 0 and again["pending"] == 0
    assert gs.docs[0] == {"_id": "g1", "guildId": "1", "revision": 0}
    assert len(gs.updated) == 1  # второй прогон не тронул ни одного документа


def test_m7_is_registered_as_additive_migration_and_skipped_when_done() -> None:
    mig7 = next((m for m in migrate.MIGRATIONS if m.id == 7), None)
    assert mig7 is not None and mig7.name == "guild-settings-revision-backfill"
    assert mig7.backward_compatible is True  # старый код работает и без revision (DB07)

    db = FakeDB()
    db[migrate.MIG_COLL].docs = [{"_id": 7, "name": mig7.name, "status": "done",
                                  "checksum": migrate.migration_checksum(mig7)}]
    plan = migrate.plan_and_apply(db, apply=False, only=7)
    assert plan["actions"] == [{"id": 7, "name": mig7.name, "action": "skip-done"}]


def test_m8_creates_presence_collection_once_under_migration_role() -> None:
    db = FakeDB()
    dry = migrate._apply_voice_presence_collection(db, dry=True)
    assert dry["created"] is False
    assert migrate.VOICE_PRESENCE_COLLECTION not in db.cols

    first = migrate._apply_voice_presence_collection(db, dry=False)
    assert first["created"] is True
    again = migrate._apply_voice_presence_collection(db, dry=False)
    assert again["created"] is False
    assert db.list_collection_names().count(migrate.VOICE_PRESENCE_COLLECTION) == 1
