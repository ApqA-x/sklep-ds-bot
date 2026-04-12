from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from voice_tracker import domain
from voice_tracker.summary import BuildSummary, FormatSummary, Service


def _ptr_time(value: datetime) -> datetime:
    return value


def test_build_and_format_summary() -> None:
    started_at = datetime(2026, 4, 5, 18, 0, 0, tzinfo=UTC)
    ended_at = started_at + timedelta(hours=1)
    session = domain.Session(id="s1", guild_id="g1", channel_id="c1", started_at=started_at, ended_at=ended_at)

    participants = [
        domain.ParticipantInterval(
            user_id="u1",
            user_name="alice",
            joined_at=started_at,
            left_at=_ptr_time(started_at + timedelta(minutes=20)),
            duration_ms=int(timedelta(minutes=20).total_seconds() * 1000),
        ),
        domain.ParticipantInterval(
            user_id="u1",
            user_name="alice",
            joined_at=started_at + timedelta(minutes=30),
            left_at=_ptr_time(started_at + timedelta(minutes=45)),
            duration_ms=0,
        ),
        domain.ParticipantInterval(
            user_id="u2",
            user_name="bob",
            joined_at=started_at + timedelta(minutes=10),
            left_at=_ptr_time(started_at + timedelta(minutes=10, milliseconds=500)),
            duration_ms=-1,
        ),
    ]

    summary = BuildSummary(session, participants, "u2")
    assert summary.unique_users == 2
    assert len(summary.participants) == 2
    assert summary.participants[0].user_id == "u1"
    assert summary.participants[1].user_id == "u2"
    assert summary.participants[0].total_time > summary.participants[1].total_time

    message = FormatSummary(summary)
    assert message
    assert "<@" not in message
    assert "Ended by:" in message
    assert "bob" in message
    assert summary.total_duration == timedelta(hours=1)
    assert summary.ended_by_user_id == "u2"


class SummaryFakeRepo:
    def __init__(self) -> None:
        self.session: domain.Session | None = None
        self.settings: domain.GuildSettings | None = None
        self.parts: list[domain.ParticipantInterval] = []

    def GetSessionByID(self, _session_id: str) -> domain.Session | None:
        return self.session

    def ListParticipantsBySession(self, _session_id: str) -> list[domain.ParticipantInterval]:
        return self.parts

    def GetGuildSettings(self, _guild_id: str) -> domain.GuildSettings | None:
        return self.settings

    def ListClosedSessionsPendingSummary(self) -> list[domain.Session]:
        if self.session is not None and self.session.status == domain.SESSION_STATUS_CLOSED and self.session.summary_generated_at is None:
            return [self.session]
        return []

    def MarkSessionSummaryReady(self, _session_id: str, _destination: str, _message: str, _ready_at: datetime) -> None:
        return None


class SummaryFakePublisher:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish_json(self, _subject: str, value: object) -> None:
        self.events.append(value)


def _must_json(value: object) -> bytes:
    return json.dumps(value, default=domain.to_jsonable).encode("utf-8")


@pytest.mark.asyncio
async def test_handle_session_closed_requires_summary_channel() -> None:
    started_at = datetime(2026, 4, 5, 18, 0, 0, tzinfo=UTC)
    ended_at = started_at + timedelta(hours=1)
    repo = SummaryFakeRepo()
    repo.session = domain.Session(id="s1", guild_id="g1", channel_id="c1", started_at=started_at, ended_at=ended_at)
    repo.parts = [
        domain.ParticipantInterval(
            user_id="u1",
            user_name="alice",
            joined_at=started_at,
            left_at=ended_at,
            duration_ms=int(timedelta(hours=1).total_seconds() * 1000),
        )
    ]
    service = Service(repo, SummaryFakePublisher())

    with pytest.raises(ValueError, match="summary channel not configured"):
        await service.HandleSessionClosed(_must_json(domain.SessionClosedEvent(session_id="s1", guild_id="g1", channel_id="c1")))


@pytest.mark.asyncio
async def test_handle_session_closed_uses_configured_summary_channel() -> None:
    started_at = datetime(2026, 4, 5, 18, 0, 0, tzinfo=UTC)
    ended_at = started_at + timedelta(hours=1)
    repo = SummaryFakeRepo()
    repo.session = domain.Session(id="s1", guild_id="g1", channel_id="c1", started_at=started_at, ended_at=ended_at)
    repo.settings = domain.GuildSettings(guild_id="g1", summary_channel_id="text-1")
    repo.parts = [
        domain.ParticipantInterval(
            user_id="u1",
            user_name="alice",
            joined_at=started_at,
            left_at=ended_at,
            duration_ms=int(timedelta(hours=1).total_seconds() * 1000),
        )
    ]
    publisher = SummaryFakePublisher()
    service = Service(repo, publisher)

    await service.HandleSessionClosed(_must_json(domain.SessionClosedEvent(session_id="s1", guild_id="g1", channel_id="c1")))
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert isinstance(event, domain.SummaryReadyEvent)
    assert event.channel_id == "text-1"


@pytest.mark.asyncio
async def test_handle_session_closed_uses_fallback_summary_channel() -> None:
    started_at = datetime(2026, 4, 5, 18, 0, 0, tzinfo=UTC)
    ended_at = started_at + timedelta(hours=1)
    repo = SummaryFakeRepo()
    repo.session = domain.Session(id="s1", guild_id="g1", channel_id="c1", started_at=started_at, ended_at=ended_at)
    repo.settings = domain.GuildSettings(guild_id="g1", fallback_summary_channel_id="text-fallback")
    repo.parts = [
        domain.ParticipantInterval(
            user_id="u1",
            user_name="alice",
            joined_at=started_at,
            left_at=ended_at,
            duration_ms=int(timedelta(hours=1).total_seconds() * 1000),
        )
    ]
    publisher = SummaryFakePublisher()
    service = Service(repo, publisher)

    await service.HandleSessionClosed(_must_json(domain.SessionClosedEvent(session_id="s1", guild_id="g1", channel_id="c1")))
    event = publisher.events[0]
    assert isinstance(event, domain.SummaryReadyEvent)
    assert event.channel_id == "text-fallback"
