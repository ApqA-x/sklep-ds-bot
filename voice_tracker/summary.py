from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import Any, Protocol

from . import domain
from .timeutil import go_duration


class Repository(Protocol):
    def GetSessionByID(self, session_id: str) -> domain.Session | None: ...

    def ListParticipantsBySession(self, session_id: str) -> list[domain.ParticipantInterval]: ...

    def GetGuildSettings(self, guild_id: str) -> domain.GuildSettings | None: ...

    def ListClosedSessionsPendingSummary(self) -> list[domain.Session]: ...

    def MarkSessionSummaryReady(self, session_id: str, destination: str, message: str, ready_at: datetime) -> None: ...


class Publisher(Protocol):
    async def publish_json(self, subject: str, value: Any) -> Any: ...


class Service:
    def __init__(self, repo: Repository, publisher: Publisher | None) -> None:
        self.repo = repo
        self.publisher = publisher

    async def HandleSessionClosed(self, payload: bytes) -> None:
        await self._generate_and_publish(payload)

    async def Start(self) -> None:
        pending = self.repo.ListClosedSessionsPendingSummary()
        for session in pending:
            try:
                await self._generate_and_publish_for_session(session)
            except Exception:
                continue

    async def _generate_and_publish(self, payload: bytes) -> None:
        event = domain.SessionClosedEvent.from_dict(json.loads(payload.decode("utf-8")))
        session = self.repo.GetSessionByID(event.session_id)
        if session is None:
            return
        if session.summary_generated_at is not None or session.summary_delivered_at is not None:
            return
        if session.guild_id != event.guild_id or session.channel_id != event.channel_id:
            raise ValueError("session mismatch for summary event")

        participants = self.repo.ListParticipantsBySession(event.session_id)
        summary = BuildSummary(session, participants, event.ended_by_user_id)
        settings = self.repo.GetGuildSettings(event.guild_id)
        destination = "" if settings is None else settings.summary_destination("")
        if destination == "":
            raise ValueError("summary channel not configured")

        message = FormatSummary(summary)
        ready_at = datetime.now(UTC)
        self.repo.MarkSessionSummaryReady(session.id, destination, message, ready_at)
        await self._publish_json(
            domain.SUBJECT_SUMMARY_READY,
            domain.SummaryReadyEvent(
                session_id=event.session_id,
                guild_id=event.guild_id,
                channel_id=destination,
                message=message,
            ),
        )

    async def _generate_and_publish_for_session(self, session: domain.Session) -> None:
        participants = self.repo.ListParticipantsBySession(session.id)
        event = domain.SessionClosedEvent(
            session_id=session.id,
            guild_id=session.guild_id,
            channel_id=session.channel_id,
            started_at=session.started_at,
            ended_at=deref_time(session.ended_at),
            ended_by_user_id=session.ended_by_user_id,
        )
        await self._generate_and_publish_from_session(session, event, participants)

    async def _generate_and_publish_from_session(
        self,
        session: domain.Session,
        event: domain.SessionClosedEvent,
        participants: list[domain.ParticipantInterval],
    ) -> None:
        if session.guild_id != event.guild_id or session.channel_id != event.channel_id:
            raise ValueError("session mismatch for summary event")
        summary = BuildSummary(session, participants, event.ended_by_user_id)
        settings = self.repo.GetGuildSettings(event.guild_id)
        destination = "" if settings is None else settings.summary_destination("")
        if destination == "":
            raise ValueError("summary channel not configured")

        message = FormatSummary(summary)
        ready_at = datetime.now(UTC)
        self.repo.MarkSessionSummaryReady(session.id, destination, message, ready_at)
        await self._publish_json(
            domain.SUBJECT_SUMMARY_READY,
            domain.SummaryReadyEvent(
                session_id=session.id,
                guild_id=event.guild_id,
                channel_id=destination,
                message=message,
            ),
        )

    async def _publish_json(self, subject: str, value: Any) -> Any:
        if self.publisher is None:
            return None
        publisher = getattr(self.publisher, "publish_json", None)
        if publisher is None:
            publisher = getattr(self.publisher, "PublishJSON", None)
        if publisher is None:
            raise AttributeError("publisher has no publish_json method")
        result = publisher(subject, value)
        if inspect.isawaitable(result):
            return await result
        return result


def BuildSummary(
    session: domain.Session,
    participants: list[domain.ParticipantInterval],
    ended_by_user_id: str,
) -> domain.SessionSummary:
    return domain.build_session_summary(session, participants, ended_by_user_id)


def FormatSummary(summary: domain.SessionSummary) -> str:
    lines = [
        "**Voice session ended**",
        f"**Channel:** <#{summary.channel_id}>",
        f"**Duration:** {go_duration(summary.total_duration, round_seconds=True)}",
        f"**Unique users:** {summary.unique_users}",
    ]
    if summary.ended_by_user_id:
        lines.append(f"**Ended by:** {participant_display_name(summary.participants, summary.ended_by_user_id)}")
    lines.append("")
    lines.append("**Participants**")
    if len(summary.participants) == 0:
        lines.append("- none")
    for participant in summary.participants:
        name = participant.user_name or participant.user_id
        lines.append(f"- {name} - {go_duration(participant.total_time, round_seconds=True)} ({interval_label(participant.intervals)})")
    return "\n".join(lines).strip()


def participant_display_name(participants: list[domain.ParticipantSummary], user_id: str) -> str:
    user_id = user_id.strip()
    if not user_id:
        return "unknown"
    for participant in participants:
        if participant.user_id == user_id and participant.user_name.strip():
            return participant.user_name
    return user_id


def interval_label(count: int) -> str:
    if count == 1:
        return "1 interval"
    return f"{count} intervals"


def deref_time(value: datetime | None) -> datetime | None:
    return value


start = Service.Start
handle_session_closed = Service.HandleSessionClosed
build_summary = BuildSummary
format_summary = FormatSummary
