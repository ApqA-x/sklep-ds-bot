from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from voice_tracker import domain
from voice_tracker.repository import Repository


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, value in query.items():
        if isinstance(value, dict):
            actual = doc.get(key)
            for op, expected in value.items():
                if op == "$in":
                    if actual not in set(expected):
                        return False
                    continue
                raise AssertionError(f"unsupported query op: {op}")
            continue
        if doc.get(key) != value:
            return False
    return True


class _DuplicateKeyError(Exception):
    code = 11000


class _UpdateResult:
    def __init__(self, matched_count: int) -> None:
        self.matched_count = matched_count


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = [dict(doc) for doc in docs]

    def to_list(self, _length: int | None) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self._docs]

    def __iter__(self):
        return iter(self.to_list(None))


class _Collection:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.index_calls: list[dict[str, Any]] = []

    def create_index(self, keys: list[tuple[str, int]], **kwargs: Any) -> str:
        self.index_calls.append({"keys": list(keys), "kwargs": dict(kwargs)})
        return f"idx_{len(self.index_calls)}"

    def insert_one(self, doc: dict[str, Any]) -> None:
        if "_id" in doc and any(existing.get("_id") == doc["_id"] for existing in self.documents):
            raise _DuplicateKeyError("duplicate key")
        self.documents.append(dict(doc))

    def find(self, query: dict[str, Any]) -> _Cursor:
        return _Cursor([doc for doc in self.documents if _matches(doc, query)])

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for doc in self.documents:
            if _matches(doc, query):
                return dict(doc)
        return None

    def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False) -> _UpdateResult:
        for idx, current in enumerate(self.documents):
            if not _matches(current, query):
                continue
            updated = dict(current)
            updated.update(update.get("$set", {}))
            self.documents[idx] = updated
            return _UpdateResult(1)
        if not upsert:
            return _UpdateResult(0)
        created = {"_id": query.get("_id")}
        created.update(update.get("$setOnInsert", {}))
        created.update(update.get("$set", {}))
        self.documents.append(created)
        return _UpdateResult(0)


class _FakeDb:
    def __init__(self) -> None:
        self.collections = {
            "guild_settings": _Collection(),
            "processed_messages": _Collection(),
            "voice_sessions": _Collection(),
            "voice_session_participants": _Collection(),
            "guild_invite_snapshots": _Collection(),
            "invite_catalog": _Collection(),
            "member_join_attributions": _Collection(),
            "member_join_state": _Collection(),
            "member_role_state": _Collection(),
            "member_nickname_state": _Collection(),
            "member_nickname_history": _Collection(),
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections[name]


def test_ensure_indexes_registers_invite_attribution_collections() -> None:
    db = _FakeDb()
    repo = Repository(db)

    repo.ensure_indexes(None)

    assert db.collections["guild_invite_snapshots"].index_calls == [
        {"keys": [("guildId", 1)], "kwargs": {"unique": True}},
        {"keys": [("capturedAt", -1)], "kwargs": {}},
    ]
    assert db.collections["invite_catalog"].index_calls == [
        {"keys": [("guildId", 1), ("code", 1)], "kwargs": {"unique": True}},
        {"keys": [("guildId", 1), ("lastSeenAt", -1)], "kwargs": {}},
        {"keys": [("guildId", 1), ("deletedAt", 1)], "kwargs": {}},
    ]
    assert db.collections["member_join_state"].index_calls == [
        {"keys": [("guildId", 1), ("userId", 1)], "kwargs": {"unique": True}},
        {"keys": [("guildId", 1), ("joinedAt", -1)], "kwargs": {}},
    ]
    assert db.collections["member_join_attributions"].index_calls == [
        {"keys": [("guildId", 1), ("userId", 1), ("joinedAt", -1)], "kwargs": {}},
        {"keys": [("guildId", 1), ("joinedAt", -1)], "kwargs": {}},
        {"keys": [("guildId", 1), ("attributionStatus", 1), ("joinedAt", -1)], "kwargs": {}},
    ]


def test_snapshot_and_catalog_round_trip_invite_metadata() -> None:
    repo = Repository(_FakeDb())
    captured_at = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    snapshot = domain.GuildInviteSnapshot(
        guild_id="g1",
        captured_at=captured_at,
        invites=[
            domain.InviteSnapshotEntry(
                code="abc123",
                uses=3,
                url="https://discord.gg/abc123",
                channel_id="c1",
                inviter_user_id="u9",
                inviter_name="Alice",
            ),
            domain.InviteSnapshotEntry(
                code="vanity",
                uses=7,
                url="https://discord.gg/brand",
                invite_type=domain.INVITE_TYPE_VANITY,
            ),
        ],
    )

    repo.upsert_guild_invite_snapshot(None, snapshot)
    repo.sync_invite_catalog_from_snapshot(None, snapshot)

    stored_snapshot = repo.get_guild_invite_snapshot(None, "g1")
    stored_catalog = repo.get_invite_catalog_entry(None, "g1", "abc123")
    vanity_catalog = repo.get_invite_catalog_entry(None, "g1", "vanity")

    assert stored_snapshot is not None
    assert stored_snapshot.guild_id == "g1"
    assert stored_snapshot.captured_at == captured_at
    assert [invite.code for invite in stored_snapshot.invites] == ["abc123", "vanity"]
    assert stored_catalog is not None
    assert stored_catalog.id == "g1:abc123"
    assert stored_catalog.created_by_user_id == "u9"
    assert stored_catalog.created_by_name == "Alice"
    assert stored_catalog.last_seen_at == captured_at
    assert stored_catalog.source == domain.INVITE_SOURCE_SNAPSHOT
    assert vanity_catalog is not None
    assert vanity_catalog.invite_type == domain.INVITE_TYPE_VANITY


def test_append_member_join_attribution_uses_deterministic_id_and_is_append_only() -> None:
    repo = Repository(_FakeDb())
    joined_at = datetime(2026, 5, 6, 13, 15, 30, tzinfo=UTC)
    attribution = domain.MemberJoinAttribution(
        guild_id="g1",
        user_id="u1",
        joined_at=joined_at,
        invite_code="abc123",
        invite_url="https://discord.gg/abc123",
        inviter_user_id="u9",
        inviter_name="Alice",
        attribution_status=domain.INVITE_ATTRIBUTION_STATUS_EXACT,
        candidate_codes=["abc123"],
        snapshot_captured_at=joined_at,
    )

    inserted = repo.append_member_join_attribution(None, attribution)
    duplicated = repo.append_member_join_attribution(None, attribution)

    assert attribution.id == "g1:u1:2026-05-06T13:15:30Z"
    assert inserted is True
    assert duplicated is False
    assert len(repo.member_join_attributions.documents) == 1
    stored = repo.member_join_attributions.documents[0]
    assert stored["_id"] == attribution.id
    assert stored["candidateCodes"] == ["abc123"]
    assert stored["source"] == domain.INVITE_SOURCE_LIVE_DIFF
    assert stored["createdAt"] is not None


def test_project_member_join_state_keeps_latest_live_attribution_only() -> None:
    repo = Repository(_FakeDb())
    earlier = domain.MemberJoinAttribution(
        guild_id="g1",
        user_id="u1",
        joined_at=datetime(2026, 5, 6, 13, 0, tzinfo=UTC),
        invite_code="old",
        invite_url="https://discord.gg/old",
        inviter_user_id="u2",
        inviter_name="Old",
        attribution_status=domain.INVITE_ATTRIBUTION_STATUS_EXACT,
    )
    latest = domain.MemberJoinAttribution(
        guild_id="g1",
        user_id="u1",
        joined_at=datetime(2026, 5, 6, 14, 0, tzinfo=UTC),
        invite_code="new",
        invite_url="https://discord.gg/new",
        inviter_user_id="u3",
        inviter_name="New",
        attribution_status=domain.INVITE_ATTRIBUTION_STATUS_AMBIGUOUS,
        candidate_codes=["new", "other"],
    )
    repaired = domain.MemberJoinAttribution(
        guild_id="g1",
        user_id="u1",
        joined_at=datetime(2026, 5, 6, 15, 0, tzinfo=UTC),
        invite_code="repair",
        invite_url="https://discord.gg/repair",
        inviter_user_id="u4",
        inviter_name="Repair",
        attribution_status=domain.INVITE_ATTRIBUTION_STATUS_EXACT,
        source=domain.INVITE_SOURCE_AUDIT_LOG,
    )

    first_state = repo.project_member_join_state(None, earlier)
    second_state = repo.project_member_join_state(None, latest)
    replayed_older_state = repo.project_member_join_state(None, earlier)
    ignored_repair = repo.project_member_join_state(None, repaired)
    stored = repo.get_member_join_state(None, "g1", "u1")

    assert first_state is not None
    assert second_state is not None
    assert replayed_older_state is not None
    assert ignored_repair is None
    assert stored is not None
    assert stored.latest_join_attribution_id == latest.id
    assert stored.joined_at == latest.joined_at
    assert stored.invite_code == "new"
    assert stored.inviter_name == "New"
    assert stored.attribution_status == domain.INVITE_ATTRIBUTION_STATUS_AMBIGUOUS
    assert len(repo.member_join_state.documents) == 1


def test_get_member_profile_returns_persisted_invite_attribution_from_join_state() -> None:
    repo = Repository(_FakeDb())
    attribution = domain.MemberJoinAttribution(
        guild_id="g1",
        user_id="u42",
        joined_at=datetime(2026, 5, 6, 16, 0, tzinfo=UTC),
        invite_code="welcome",
        invite_url="https://discord.gg/welcome",
        invite_type=domain.INVITE_TYPE_VANITY,
        inviter_user_id="u99",
        inviter_name="Greeter",
        attribution_status=domain.INVITE_ATTRIBUTION_STATUS_UNKNOWN,
    )

    repo.project_member_join_state(None, attribution)

    profile = repo.get_member_profile(None, "g1", "u42")

    assert profile == {
        "user_id": "u42",
        "user_name": "",
        "total_for": 0,
        "roles": [],
        "nickname": "",
        "nickname_history": [],
        "latest_join_attribution_id": attribution.id,
        "joined_at": datetime(2026, 5, 6, 16, 0, tzinfo=UTC),
        "invite_code": "welcome",
        "invite_url": "https://discord.gg/welcome",
        "invite_type": domain.INVITE_TYPE_VANITY,
        "inviter_user_id": "u99",
        "inviter_name": "Greeter",
        "attribution_status": domain.INVITE_ATTRIBUTION_STATUS_UNKNOWN,
    }
