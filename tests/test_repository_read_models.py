from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from voice_tracker import commands as command_service
from voice_tracker import domain
from voice_tracker.discord_models import (
    ApplicationCommandInteractionDataOption,
    Interaction,
    InteractionCreate,
    Member,
    User,
)
from voice_tracker.repository import Repository


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, value in query.items():
        if key == "$or":
            if not any(_matches(doc, branch) for branch in value):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, branch) for branch in value):
                return False
            continue
        if isinstance(value, dict):
            actual = doc.get(key)
            for op, expected in value.items():
                if op == "$in":
                    if actual not in set(expected):
                        return False
                    continue
                if op == "$exists":
                    if bool(expected) != (key in doc):
                        return False
                    continue
                raise AssertionError(f"unsupported query op: {op}")
            continue
        if doc.get(key) != value:
            return False
    return True


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = [dict(doc) for doc in docs]

    def sort(self, spec: list[tuple[str, int]]):
        for key, direction in reversed(spec):
            reverse = direction < 0
            self._docs.sort(key=lambda item: item.get(key), reverse=reverse)
        return self

    def limit(self, value: int):
        self._docs = self._docs[: max(0, int(value))]
        return self

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
        self.documents.append(dict(doc))

    def find(self, query: dict[str, Any]) -> _Cursor:
        return _Cursor([doc for doc in self.documents if _matches(doc, query)])

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for doc in self.documents:
            if _matches(doc, query):
                return dict(doc)
        return None


class _FakeDb:
    def __init__(self) -> None:
        self.collections = {
            "guild_settings": _Collection(),
            "processed_messages": _Collection(),
            "voice_sessions": _Collection(),
            "voice_session_participants": _Collection(),
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections[name]


def _insert_session(
    repo: Repository,
    *,
    session_id: str,
    guild_id: str,
    channel_id: str,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    repo.sessions.insert_one(
        domain.Session(
            id=session_id,
            guild_id=guild_id,
            channel_id=channel_id,
            status=domain.SESSION_STATUS_CLOSED,
            started_at=started_at,
            ended_at=ended_at,
        ).to_mongo()
    )


def _insert_participant(
    repo: Repository,
    *,
    participant_id: str,
    session_id: str,
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_name: str,
    joined_at: datetime,
    left_at: datetime | None,
    duration_ms: int,
    active: bool = False,
) -> None:
    repo.participants.insert_one(
        domain.ParticipantInterval(
            id=participant_id,
            session_id=session_id,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            joined_at=joined_at,
            left_at=left_at,
            duration_ms=duration_ms,
            active=active,
        ).to_mongo()
    )


def _interaction(guild_id: str = "g1", user_id: str = "invoker") -> InteractionCreate:
    return InteractionCreate(interaction=Interaction(guild_id=guild_id, member=Member(user=User(id=user_id))))


def test_list_voice_totals_by_guild_aggregates_and_orders_stably() -> None:
    repo = Repository(_FakeDb())
    base = datetime(2026, 4, 5, 18, 0, tzinfo=UTC)
    _insert_session(
        repo,
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        started_at=base,
        ended_at=base + timedelta(minutes=10),
    )

    # Uses session end as closed interval end when participant has no leftAt.
    _insert_participant(
        repo,
        participant_id="p4",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u4",
        user_name="Mia",
        joined_at=base + timedelta(minutes=5),
        left_at=None,
        duration_ms=0,
    )
    _insert_participant(
        repo,
        participant_id="p2",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u2",
        user_name="alex",
        joined_at=base,
        left_at=base + timedelta(minutes=1),
        duration_ms=60_000,
    )
    _insert_participant(
        repo,
        participant_id="p3",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u3",
        user_name="Alex",
        joined_at=base,
        left_at=base + timedelta(minutes=1),
        duration_ms=60_000,
    )
    _insert_participant(
        repo,
        participant_id="p1",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u1",
        user_name="zed",
        joined_at=base,
        left_at=base + timedelta(minutes=1),
        duration_ms=60_000,
    )
    # Negative closed interval is clamped to zero.
    _insert_participant(
        repo,
        participant_id="p5",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u5",
        user_name="NoTime",
        joined_at=base + timedelta(minutes=3),
        left_at=base + timedelta(minutes=2),
        duration_ms=0,
    )

    rows = repo.list_voice_totals_by_guild(None, "g1")

    assert [row["user_id"] for row in rows] == ["u4", "u2", "u3", "u1", "u5"]
    totals = {row["user_id"]: row["total_for"] for row in rows}
    assert totals["u4"] == 300_000
    assert totals["u2"] == 60_000
    assert totals["u3"] == 60_000
    assert totals["u1"] == 60_000
    assert totals["u5"] == 0


def test_get_member_profile_and_alias_return_aggregated_voice_totals() -> None:
    repo = Repository(_FakeDb())
    base = datetime(2026, 4, 5, 18, 0, tzinfo=UTC)
    _insert_session(
        repo,
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        started_at=base,
        ended_at=base + timedelta(minutes=10),
    )
    _insert_session(
        repo,
        session_id="s2",
        guild_id="g1",
        channel_id="c2",
        started_at=base + timedelta(minutes=10),
        ended_at=base + timedelta(minutes=20),
    )
    _insert_participant(
        repo,
        participant_id="p1",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u1",
        user_name="OldName",
        joined_at=base,
        left_at=base + timedelta(minutes=1),
        duration_ms=60_000,
    )
    _insert_participant(
        repo,
        participant_id="p2",
        session_id="s2",
        guild_id="g1",
        channel_id="c2",
        user_id="u1",
        user_name="NewName",
        joined_at=base + timedelta(minutes=18),
        left_at=None,
        duration_ms=0,
    )

    profile = repo.get_member_profile(None, "g1", "u1")

    assert profile == {"user_id": "u1", "user_name": "NewName", "total_for": 180_000, "roles": []}
    assert repo.get_user_voice_summary(None, "g1", "u1") == profile
    assert repo.get_member_profile(None, "g1", "missing") is None


def test_list_voice_totals_by_guild_keeps_empty_user_name_when_repository_has_no_name() -> None:
    repo = Repository(_FakeDb())
    base = datetime(2026, 4, 5, 18, 0, tzinfo=UTC)
    _insert_session(
        repo,
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        started_at=base,
        ended_at=base + timedelta(minutes=10),
    )
    _insert_participant(
        repo,
        participant_id="p1",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u1",
        user_name="",
        joined_at=base,
        left_at=base + timedelta(minutes=2),
        duration_ms=120_000,
    )

    rows = repo.list_voice_totals_by_guild(None, "g1")

    assert rows == [{"user_id": "u1", "user_name": "", "total_for": 120_000}]


def test_get_member_profile_keeps_empty_user_name_when_repository_has_no_name() -> None:
    repo = Repository(_FakeDb())
    base = datetime(2026, 4, 5, 18, 0, tzinfo=UTC)
    _insert_session(
        repo,
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        started_at=base,
        ended_at=base + timedelta(minutes=10),
    )
    _insert_participant(
        repo,
        participant_id="p1",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u1",
        user_name="",
        joined_at=base,
        left_at=base + timedelta(minutes=3),
        duration_ms=180_000,
    )

    profile = repo.get_member_profile(None, "g1", "u1")

    assert profile == {"user_id": "u1", "user_name": "", "total_for": 180_000, "roles": []}
    assert repo.get_user_voice_summary(None, "g1", "u1") == profile


def test_dashboard_and_userinfo_read_from_repository_aggregates() -> None:
    repo = Repository(_FakeDb())
    base = datetime(2026, 4, 5, 18, 0, tzinfo=UTC)
    _insert_session(
        repo,
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        started_at=base,
        ended_at=base + timedelta(minutes=4),
    )
    _insert_participant(
        repo,
        participant_id="p1",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u1",
        user_name="Alice",
        joined_at=base,
        left_at=base + timedelta(minutes=1),
        duration_ms=60_000,
    )
    _insert_participant(
        repo,
        participant_id="p2",
        session_id="s1",
        guild_id="g1",
        channel_id="c1",
        user_id="u2",
        user_name="Bob",
        joined_at=base,
        left_at=base + timedelta(minutes=2),
        duration_ms=120_000,
    )

    svc = command_service.Service(repo)
    interaction = _interaction("g1")
    dashboard = svc.handle_dashboard_command(None, interaction, "", [])
    userinfo = svc.handle_userinfo_command(
        None,
        interaction,
        "",
        [ApplicationCommandInteractionDataOption(name="user", value="u2")],
    )

    assert dashboard.splitlines()[:3] == [
        "Voice dashboard",
        "1. Bob: 0:02:00",
        "2. Alice: 0:01:00",
    ]
    assert userinfo.splitlines() == ["User: Bob", "Total voice time: 0:02:00"]

def test_ensure_indexes_adds_repository_read_model_indexes() -> None:
    db = _FakeDb()
    repo = Repository(db)

    repo.ensure_indexes(None)

    participant_indexes = [call["keys"] for call in db.collections["voice_session_participants"].index_calls]
    assert [("guildId", 1), ("userId", 1), ("sessionId", 1)] in participant_indexes
    assert [("guildId", 1), ("userId", 1), ("joinedAt", -1)] in participant_indexes
