from __future__ import annotations

import inspect
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from . import domain


@dataclass(slots=True)
class Defaults:
    tracking_mode: str = ""
    tracked_channel_ids: list[str] = field(default_factory=list)


class Repository(Protocol):
    def GetGuildSettings(self, guild_id: str) -> domain.GuildSettings | None: ...

    def CreateSession(self, session: domain.Session) -> None: ...

    def FindActiveSession(self, guild_id: str, channel_id: str) -> domain.Session | None: ...

    def ListActiveSessions(self) -> list[domain.Session]: ...

    def ListClosedSessionsPendingNotification(self) -> list[domain.Session]: ...

    def GetSessionByID(self, session_id: str) -> domain.Session | None: ...

    def CloseSession(self, session_id: str, ended_at: datetime, ended_by_user_id: str) -> None: ...

    def MarkSessionClosedEventPublished(self, session_id: str, published_at: datetime) -> None: ...

    def CreateParticipant(self, participant: domain.ParticipantInterval) -> None: ...

    def FindActiveParticipant(self, session_id: str, user_id: str) -> domain.ParticipantInterval | None: ...

    def ListActiveParticipants(self, session_id: str) -> list[domain.ParticipantInterval]: ...

    def ListParticipantsBySession(self, session_id: str) -> list[domain.ParticipantInterval]: ...

    def CloseParticipant(self, participant_id: str, left_at: datetime, duration_ms: int) -> None: ...


class Publisher(Protocol):
    async def publish_json(self, subject: str, value: Any) -> Any: ...


@dataclass(slots=True)
class _ActiveParticipant:
    id: str
    user_id: str
    user_name: str
    joined_at: datetime


@dataclass(slots=True)
class _ActiveSession:
    session: domain.Session
    participants: dict[str, _ActiveParticipant] = field(default_factory=dict)
    unique_users: set[str] = field(default_factory=set)


class Service:
    def __init__(self, repo: Repository | None, publisher: Publisher | None, defaults: Defaults | None = None) -> None:
        defaults = defaults or Defaults()
        self.repo = repo
        self.publisher = publisher
        self.defaults = domain.new_guild_settings(
            "",
            defaults.tracking_mode,
            defaults.tracked_channel_ids,
            "",
        ).canonical_for_voice_tracking()
        self._lock = threading.Lock()
        self._sessions: dict[str, _ActiveSession] = {}

    async def Start(self) -> None:
        if self.repo is None:
            return

        active = self.repo.ListActiveSessions()
        for session in active:
            try:
                participants = self.repo.ListActiveParticipants(session.id)
                if len(participants) == 0:
                    all_participants = self.repo.ListParticipantsBySession(session.id)
                    ended_at, ended_by_user_id = latest_participant_end(all_participants)
                    if ended_at is None:
                        ended_at = datetime.now(UTC)
                    self.repo.CloseSession(session.id, ended_at, ended_by_user_id)
                    if self.publisher is not None:
                        try:
                            await self._publish_json(
                                domain.SUBJECT_SESSION_CLOSED,
                                domain.SessionClosedEvent(
                                    session_id=session.id,
                                    guild_id=session.guild_id,
                                    channel_id=session.channel_id,
                                    started_at=session.started_at,
                                    ended_at=ended_at,
                                    ended_by_user_id=ended_by_user_id,
                                ),
                            )
                        except Exception:
                            continue
                        try:
                            self.repo.MarkSessionClosedEventPublished(session.id, datetime.now(UTC))
                        except Exception:
                            continue
                    continue

                state = _ActiveSession(session=session)
                for participant in participants:
                    state.participants[participant.user_id] = _ActiveParticipant(
                        id=participant.id,
                        user_id=participant.user_id,
                        user_name=participant.user_name,
                        joined_at=participant.joined_at,
                    )
                    state.unique_users.add(participant.user_id)
                with self._lock:
                    self._sessions[_session_key(session.guild_id, session.channel_id)] = state
            except Exception:
                continue

        pending = self.repo.ListClosedSessionsPendingNotification()
        for session in pending:
            if self.publisher is None:
                continue
            closed = domain.SessionClosedEvent(
                session_id=session.id,
                guild_id=session.guild_id,
                channel_id=session.channel_id,
                started_at=session.started_at,
                ended_at=deref_time(session.ended_at),
                ended_by_user_id=session.ended_by_user_id,
            )
            try:
                await self._publish_json(domain.SUBJECT_SESSION_CLOSED, closed)
            except Exception:
                continue
            try:
                self.repo.MarkSessionClosedEventPublished(session.id, datetime.now(UTC))
            except Exception:
                continue

    async def HandleVoiceEvent(self, event: domain.VoiceStateEvent) -> None:
        if event.is_bot:
            return
        if event.occurred_at is None:
            event.occurred_at = datetime.now(UTC)

        if event.previous_channel_id and event.previous_channel_id != event.channel_id:
            closed = self._leave(event.guild_id, event.previous_channel_id, event.user_id, event.occurred_at)
            if closed is not None:
                await self._publish_closed(closed)
                self.repo.MarkSessionClosedEventPublished(closed.session_id, datetime.now(UTC))
            if event.channel_id:
                await self._join(event)
            return

        if not event.channel_id:
            closed = self._leave(event.guild_id, event.previous_channel_id, event.user_id, event.occurred_at)
            if closed is not None:
                await self._publish_closed(closed)
                self.repo.MarkSessionClosedEventPublished(closed.session_id, datetime.now(UTC))
            return

        await self._join(event)

    async def _join(self, event: domain.VoiceStateEvent) -> None:
        settings = self._settings_for_guild(event.guild_id)
        if not settings.tracks_channel(event.channel_id):
            return

        key = _session_key(event.guild_id, event.channel_id)
        with self._lock:
            state = self._sessions.get(key)
            if state is None:
                session = domain.Session(
                    id=str(uuid.uuid4()),
                    guild_id=event.guild_id,
                    channel_id=event.channel_id,
                    status=domain.SESSION_STATUS_ACTIVE,
                    started_at=event.occurred_at,
                    created_at=event.occurred_at,
                    updated_at=event.occurred_at,
                )
                self.repo.CreateSession(session)
                state = _ActiveSession(session=session)
                self._sessions[key] = state

            if event.user_id in state.participants:
                return

            participant = domain.ParticipantInterval(
                id=str(uuid.uuid4()),
                session_id=state.session.id,
                guild_id=event.guild_id,
                channel_id=event.channel_id,
                user_id=event.user_id,
                user_name=event.user_name,
                joined_at=event.occurred_at,
                active=True,
            )
            self.repo.CreateParticipant(participant)
            state.participants[event.user_id] = _ActiveParticipant(
                id=participant.id,
                user_id=participant.user_id,
                user_name=participant.user_name,
                joined_at=participant.joined_at or event.occurred_at,
            )
            state.unique_users.add(event.user_id)

    def _leave(
        self,
        guild_id: str,
        channel_id: str,
        user_id: str,
        occurred_at: datetime,
    ) -> domain.SessionClosedEvent | None:
        settings = self._settings_for_guild(guild_id)
        if not channel_id or not settings.tracks_channel(channel_id):
            return None

        key = _session_key(guild_id, channel_id)
        with self._lock:
            state = self._sessions.get(key)
            if state is not None:
                participant = state.participants.get(user_id)
                if participant is not None:
                    duration_ms = int((occurred_at - participant.joined_at).total_seconds() * 1000)
                    if duration_ms < 0:
                        duration_ms = 0
                    self.repo.CloseParticipant(participant.id, occurred_at, duration_ms)
                    del state.participants[user_id]

                    if state.participants:
                        return None

                    self.repo.CloseSession(state.session.id, occurred_at, user_id)
                    del self._sessions[key]
                    return domain.SessionClosedEvent(
                        session_id=state.session.id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        started_at=state.session.started_at,
                        ended_at=occurred_at,
                        ended_by_user_id=user_id,
                    )

        return self._leave_from_repository(guild_id, channel_id, user_id, occurred_at, key)

    def _leave_from_repository(
        self,
        guild_id: str,
        channel_id: str,
        user_id: str,
        occurred_at: datetime,
        key: str,
    ) -> domain.SessionClosedEvent | None:
        session = self.repo.FindActiveSession(guild_id, channel_id)
        if session is None:
            return None

        participant = self.repo.FindActiveParticipant(session.id, user_id)
        if participant is None:
            return None

        joined_at = participant.joined_at or occurred_at
        duration_ms = int((occurred_at - joined_at).total_seconds() * 1000)
        if duration_ms < 0:
            duration_ms = 0
        self.repo.CloseParticipant(participant.id, occurred_at, duration_ms)

        if self.repo.ListActiveParticipants(session.id):
            return None

        self.repo.CloseSession(session.id, occurred_at, user_id)
        with self._lock:
            self._sessions.pop(key, None)
        return domain.SessionClosedEvent(
            session_id=session.id,
            guild_id=guild_id,
            channel_id=channel_id,
            started_at=session.started_at,
            ended_at=occurred_at,
            ended_by_user_id=user_id,
        )

    def _settings_for_guild(self, guild_id: str) -> domain.GuildSettings:
        if self.repo is not None:
            settings = self.repo.GetGuildSettings(guild_id)
            if settings is not None:
                return settings.canonical_for_voice_tracking()
        defaults = domain.new_guild_settings(
            guild_id,
            self.defaults.tracking_mode,
            self.defaults.tracked_channel_ids,
            self.defaults.summary_channel_id,
        )
        return defaults.canonical_for_voice_tracking()

    async def _publish_closed(self, closed: domain.SessionClosedEvent) -> None:
        await self._publish_json(domain.SUBJECT_SESSION_CLOSED, closed)

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


def decode_voice_event(data: bytes) -> domain.VoiceStateEvent:
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:  # pragma: no cover - exercised by callers
        raise ValueError(f"decode voice event: {exc}") from exc
    return domain.VoiceStateEvent.from_dict(payload)


def latest_participant_end(participants: list[domain.ParticipantInterval]) -> tuple[datetime | None, str]:
    ended_at: datetime | None = None
    ended_by_user_id = ""
    for participant in participants:
        if participant.left_at is None:
            continue
        if ended_at is None or participant.left_at > ended_at:
            ended_at = participant.left_at
            ended_by_user_id = participant.user_id
    return ended_at, ended_by_user_id


def deref_time(value: datetime | None) -> datetime | None:
    return value


def _session_key(guild_id: str, channel_id: str) -> str:
    return f"{guild_id}:{channel_id}"


start = Service.Start
handle_voice_event = Service.HandleVoiceEvent
