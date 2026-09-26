"""T10 unit: канонический манифест индексов и его сверка.

DB01 — эквивалентный индекс под другим именем принимается без пересоздания;
DB02 — отличие unique/partial/TTL при тех же ключах — несовместимость, она
не скрывается; манифест покрывает ровно то, что создаёт легаси ensure_indexes.
"""

from __future__ import annotations

import json

import pytest

from voice_tracker import schema
from voice_tracker.repository import Repository


class RecCol:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[tuple, dict]] = []

    def create_index(self, keys, **kw):  # noqa: ANN001 - fake
        self.calls.append((tuple(tuple(k) for k in keys), kw))
        return kw.get("name") or schema.auto_index_name(keys)

    def update_many(self, *args, **kwargs) -> None:
        return None


class RecDB:
    def __init__(self) -> None:
        self.cols: dict[str, RecCol] = {}

    def __getitem__(self, name: str) -> RecCol:
        return self.cols.setdefault(name, RecCol(name))


def _legacy_specs(db: RecDB) -> set[tuple]:
    out = set()
    for coll, col in db.cols.items():
        for keys, kw in col.calls:
            spec = schema.IndexSpec(
                collection=coll,
                keys=keys,
                name=kw.get("name") or schema.auto_index_name(keys),
                unique=kw.get("unique", False),
                sparse=kw.get("sparse", False),
                partial=kw.get("partialFilterExpression"),
                ttl=kw.get("expireAfterSeconds"),
            )
            out.add(schema.spec_signature(spec))
    return out


def test_manifest_covers_legacy_ensure_indexes_exactly() -> None:
    db = RecDB()
    Repository(db).ensure_indexes(None)
    legacy = _legacy_specs(db)
    manifest = {schema.spec_signature(s) for s in schema.MANIFEST if s.owner in ("bot", "shared")}
    assert legacy == manifest, (
        f"легаси создаёт вне манифеста: {legacy - manifest}; "
        f"манифест не создаётся легаси: {manifest - legacy}"
    )


def test_web_subset_matches_web_indexes_list() -> None:
    """web-часть манифеста (shared включительно) — ровно то, что создаёт web ensure."""
    manifest = [s for s in schema.MANIFEST if s.owner in ("web", "shared")]
    pairs = {(s.collection, s.key_str(), s.name) for s in manifest}
    # ожидаемое зеркалит wt-web/api/queries.py WEB_INDEXES (синхронность — тестом web-репо)
    expected = {
        ("voice_session_participants", "guildId_1_joinedAt_1", "web_guildId_joinedAt"),
        ("voice_sessions", "guildId_1_status_1_endedAt_-1", "web_guildId_status_endedAt"),
        ("web_audit_logs", "guildId_1_at_-1", "web_audit_guildId_at"),
        ("discord_audit_logs", "guildId_1_at_-1", "web_disc_audit_guildId_at"),
        ("discord_audit_logs", "guildId_1_entryId_1", "web_disc_audit_guildId_entryId"),
        ("chat_messages", "guildId_1_channelId_1_sentAt_-1", "chat_guildId_channelId_sentAt"),
        ("chat_presets", "guildId_1_createdAt_1", "chat_presets_guildId_createdAt"),
        ("operations", "guildId_1_batchId_1", "web_operations_guildId_batchId"),
        ("operations", "guildId_1_createdAt_-1", "web_operations_guildId_createdAt"),
    }
    assert pairs == expected


def test_manifest_checksum_stable_and_drift_sensitive() -> None:
    first = schema.manifest_checksum()
    assert first == schema.manifest_checksum()
    altered = schema.IndexSpec("x", (("a", 1),), "a_1", owner="bot")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(schema, "MANIFEST", schema.MANIFEST + (altered,))
        assert schema.manifest_checksum() != first


def test_deployed_manifest_file_in_sync_with_code() -> None:
    """deploy/schema_manifest.json — канонический артефакт (копия уходит в web-репо);
    рассинхрон с кодом ловится здесь, а не на проде."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "deploy" / "schema_manifest.json"
    assert path.read_text(encoding="utf-8") == schema.manifest_json()


def test_legacy_prod_indexes_documented_not_recreated() -> None:
    """T10.7: обнаруженные снимком прод-индексы вне кода — owner=legacy:
    манифест их документирует, runner не создаёт, startup не требует."""
    legacy = [s for s in schema.MANIFEST if s.owner == "legacy"]
    assert {(s.collection, s.name) for s in legacy} == {
        ("voice_session_participants", "web_guild_active_user"),
        ("voice_session_participants", "web_guild_joinedAt"),
    }
    assert all(s.owner not in ("bot", "shared") for s in legacy)


def _index_doc(name: str, keys: dict, **kw) -> dict:
    return {"name": name, "key": keys, **kw}


def _db_with(indexes_by_coll: dict[str, list[dict]]) -> object:
    class Col:
        def __init__(self, docs: list[dict]) -> None:
            self.docs = docs

        def list_indexes(self):
            return iter(self.docs)

    class Db:
        def __getitem__(self, name: str) -> Col:
            return Col(indexes_by_coll.get(name, []))

    return Db()


def test_db01_equivalent_index_under_other_name_is_accepted() -> None:
    spec = next(s for s in schema.MANIFEST if s.name == "web_guildId_status_endedAt")
    db = _db_with({spec.collection: [_index_doc("web_guild_status_endedAt",
                                                {"guildId": 1, "status": 1, "endedAt": -1})]})
    report = schema.verify_db(db, owners=("web",)).raise_if_incompatible()
    assert not report.incompatible
    assert f"{spec.collection}.{spec.name}" in report.matched
    assert f"{spec.collection}.{spec.name}~(web_guild_status_endedAt)" in report.under_other_name
    # этот индекс НЕ в missing — эквивалент под старым именем принят как есть
    assert not [m for m in report.missing if m.startswith("voice_sessions.")]


def test_db02_ttl_mismatch_is_incompatible_not_hidden() -> None:
    spec = next(s for s in schema.MANIFEST if s.name == "createdAt_1" and s.collection == "processed_messages")
    db = _db_with({spec.collection: [_index_doc("createdAt_1", {"createdAt": 1}, expireAfterSeconds=3600)]})
    report = schema.verify_db(db, owners=("bot",))
    assert report.incompatible and "expireAfterSeconds" in report.incompatible[0]
    with pytest.raises(schema.SchemaIncompatible):
        report.raise_if_incompatible()


def test_db02_unique_mismatch_is_incompatible() -> None:
    spec = next(s for s in schema.MANIFEST if s.collection == "invite_catalog" and s.unique)
    db = _db_with({spec.collection: [_index_doc(spec.name, {"guildId": 1, "code": 1})]})
    report = schema.verify_db(db, owners=("bot",))
    assert report.incompatible and "unique" in report.incompatible[0]


def test_db02_partial_mismatch_is_incompatible() -> None:
    spec = next(s for s in schema.MANIFEST if s.name == "status_1_guildId_1_channelId_1")
    db = _db_with({spec.collection: [_index_doc(
        "status_1_guildId_1_channelId_1",
        {"status": 1, "guildId": 1, "channelId": 1},
        unique=True,
        partialFilterExpression={"status": "closed"},  # не canonical active
    )]})
    report = schema.verify_db(db, owners=("bot",))
    assert report.incompatible and "partialFilterExpression" in report.incompatible[0]


def test_missing_reported_but_not_confused_with_incompatible() -> None:
    db = _db_with({})
    report = schema.verify_db(db, owners=("bot", "shared"))
    assert not report.incompatible
    assert len(report.missing) > 40  # пустая БД → весь контракт отсутствует
    assert report.raise_if_incompatible() is report  # missing не блокирует (это дело runner'а)


def test_fake_db_without_list_indexes_skips_verification() -> None:
    db = RecDB()
    report = schema.verify_db(db)
    assert report.ok and report.matched == ()  # skip без имитации


def test_runner_owner_specs_excluded_from_app_startup_check() -> None:
    db = _db_with({})
    report = schema.verify_db(db, owners=("bot", "web", "shared"))
    joined = ";".join(report.missing)
    assert "operations_createdAt_ttl" not in joined
    assert "discord_audit_guildId_entryId_unique" not in joined


def test_snapshot_indexes_contains_no_documents() -> None:
    class Col:
        def list_indexes(self):
            return iter([_index_doc("_id_", {"_id": 1}), _index_doc("createdAt_1", {"createdAt": 1}, expireAfterSeconds=7200)])

    class Db:
        def __getitem__(self, _name: str) -> Col:
            return Col()

    snap = schema.snapshot_indexes(Db(), include_empty=True)
    text = json.dumps(snap, default=str)
    assert snap["schemaVersion"] == schema.SCHEMA_VERSION
    assert "voice_sessions" in snap["collections"]
    assert "documents" not in text and "guildId\": " not in text  # только метаданные индексов
