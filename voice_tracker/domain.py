from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .timeutil import datetime_to_json, ensure_utc, parse_datetime, positive_delta

SUBJECT_VOICE_EVENT = "voice.events"
SUBJECT_SESSION_CLOSED = "session.closed"
SUBJECT_SUMMARY_READY = "session.summary"

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_CLOSED = "closed"

GUILD_TRACKING_MODE_ALL = "all"
GUILD_TRACKING_MODE_NONE = "none"
GUILD_TRACKING_MODE_SPECIFIC = "specific"


def _clean(value: str | None) -> str:
    return (value or "").strip()


def clean_channel_ids(ids: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in ids or []:
        channel_id = _clean(raw)
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        out.append(channel_id)
    return sorted(out)


def normalize_tracking_mode(mode: str | None) -> str:
    value = _clean(mode).lower()
    if value in {GUILD_TRACKING_MODE_ALL, GUILD_TRACKING_MODE_NONE, GUILD_TRACKING_MODE_SPECIFIC}:
        return value
    return GUILD_TRACKING_MODE_ALL


@dataclass(slots=True)
class GuildSettings:
    guild_id: str = ""
    tracking_mode: str = GUILD_TRACKING_MODE_ALL
    tracked_channel_ids: list[str] = field(default_factory=list)
    summary_channel_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fallback_summary_channel_id: str = ""
    auto_role_id: str = ""
    auto_unmute_user_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.guild_id = _clean(self.guild_id)
        self.tracking_mode = normalize_tracking_mode(self.tracking_mode)
        self.tracked_channel_ids = clean_channel_ids(self.tracked_channel_ids)
        self.summary_channel_id = _clean(self.summary_channel_id)
        self.fallback_summary_channel_id = _clean(self.fallback_summary_channel_id)
        self.auto_role_id = _clean(self.auto_role_id)
        self.auto_unmute_user_ids = clean_channel_ids(self.auto_unmute_user_ids)
        self.created_at = ensure_utc(self.created_at)
        self.updated_at = ensure_utc(self.updated_at)

    def tracks_channel(self, channel_id: str) -> bool:
        channel_id = _clean(channel_id)
        if not channel_id:
            return False
        mode = normalize_tracking_mode(self.tracking_mode)
        if mode == GUILD_TRACKING_MODE_NONE:
            return False
        if mode == GUILD_TRACKING_MODE_ALL:
            return True
        if mode == GUILD_TRACKING_MODE_SPECIFIC:
            return channel_id in clean_channel_ids(self.tracked_channel_ids)
        return True

    def summary_destination(self, fallback_channel_id: str) -> str:
        return _clean(self.summary_channel_id) or _clean(self.fallback_summary_channel_id) or _clean(fallback_channel_id)

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "GuildSettings | None":
        if data is None:
            return None
        return cls(
            guild_id=data.get("_id") or data.get("guildId", ""),
            tracking_mode=data.get("trackingMode", GUILD_TRACKING_MODE_ALL),
            tracked_channel_ids=list(data.get("trackedChannelIds") or []),
            summary_channel_id=data.get("summaryChannelId", ""),
            fallback_summary_channel_id=data.get("fallbackSummaryChannelId", ""),
            auto_role_id=data.get("autoRoleId", ""),
            auto_unmute_user_ids=list(data.get("autoUnmuteUserIds") or []),
            created_at=parse_datetime(data.get("createdAt")),
            updated_at=parse_datetime(data.get("updatedAt")),
        )


def new_guild_settings(
    guild_id: str,
    tracking_mode: str,
    tracked_channel_ids: list[str] | None,
    summary_channel_id: str,
    fallback_summary_channel_id: str = "",
    auto_role_id: str = "",
    auto_unmute_user_ids: list[str] | None = None,
) -> GuildSettings:
    return GuildSettings(
        guild_id,
        tracking_mode,
        tracked_channel_ids or [],
        summary_channel_id,
        fallback_summary_channel_id=fallback_summary_channel_id,
        auto_role_id=auto_role_id,
        auto_unmute_user_ids=auto_unmute_user_ids or [],
    )


@dataclass(slots=True)
class VoiceStateEvent:
    guild_id: str = ""
    user_id: str = ""
    user_name: str = ""
    channel_id: str = ""
    previous_channel_id: str = ""
    is_bot: bool = False
    occurred_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VoiceStateEvent":
        return cls(
            guild_id=data.get("guildId", ""),
            user_id=data.get("userId", ""),
            user_name=data.get("userName", ""),
            channel_id=data.get("channelId", ""),
            previous_channel_id=data.get("previousChannelId", ""),
            is_bot=bool(data.get("isBot", False)),
            occurred_at=parse_datetime(data.get("occurredAt")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "guildId": self.guild_id,
            "userId": self.user_id,
            "userName": self.user_name,
            "channelId": self.channel_id,
            "previousChannelId": self.previous_channel_id,
            "isBot": self.is_bot,
            "occurredAt": datetime_to_json(self.occurred_at),
        }


@dataclass(slots=True)
class SessionClosedEvent:
    session_id: str = ""
    guild_id: str = ""
    channel_id: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    ended_by_user_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionClosedEvent":
        return cls(
            session_id=data.get("sessionId", ""),
            guild_id=data.get("guildId", ""),
            channel_id=data.get("channelId", ""),
            started_at=parse_datetime(data.get("startedAt")),
            ended_at=parse_datetime(data.get("endedAt")),
            ended_by_user_id=data.get("endedByUserId", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "guildId": self.guild_id,
            "channelId": self.channel_id,
            "startedAt": datetime_to_json(self.started_at),
            "endedAt": datetime_to_json(self.ended_at),
            "endedByUserId": self.ended_by_user_id,
        }


@dataclass(slots=True)
class SummaryReadyEvent:
    session_id: str = ""
    guild_id: str = ""
    channel_id: str = ""
    message: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummaryReadyEvent":
        return cls(
            session_id=data.get("sessionId", ""),
            guild_id=data.get("guildId", ""),
            channel_id=data.get("channelId", ""),
            message=data.get("message", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "guildId": self.guild_id,
            "channelId": self.channel_id,
            "message": self.message,
        }


@dataclass(slots=True)
class Session:
    id: str = ""
    guild_id: str = ""
    channel_id: str = ""
    status: str = SESSION_STATUS_ACTIVE
    started_at: datetime | None = None
    ended_at: datetime | None = None
    ended_by_user_id: str = ""
    closed_event_published_at: datetime | None = None
    summary_channel_id: str = ""
    summary_message: str = ""
    summary_generated_at: datetime | None = None
    summary_delivery_claimed_at: datetime | None = None
    summary_delivered_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "Session | None":
        if data is None:
            return None
        return cls(
            id=data.get("_id") or data.get("id", ""),
            guild_id=data.get("guildId", ""),
            channel_id=data.get("channelId", ""),
            status=data.get("status", SESSION_STATUS_ACTIVE),
            started_at=parse_datetime(data.get("startedAt")),
            ended_at=parse_datetime(data.get("endedAt")),
            ended_by_user_id=data.get("endedByUserId", ""),
            closed_event_published_at=parse_datetime(data.get("closedEventPublishedAt")),
            summary_channel_id=data.get("summaryChannelId", ""),
            summary_message=data.get("summaryMessage", ""),
            summary_generated_at=parse_datetime(data.get("summaryGeneratedAt")),
            summary_delivery_claimed_at=parse_datetime(data.get("summaryDeliveryClaimedAt")),
            summary_delivered_at=parse_datetime(data.get("summaryDeliveredAt")),
            created_at=parse_datetime(data.get("createdAt")),
            updated_at=parse_datetime(data.get("updatedAt")),
        )

    def to_mongo(self) -> dict[str, Any]:
        data = {
            "_id": self.id,
            "guildId": self.guild_id,
            "channelId": self.channel_id,
            "status": self.status,
            "startedAt": self.started_at,
            "endedByUserId": self.ended_by_user_id,
            "summaryChannelId": self.summary_channel_id,
            "summaryMessage": self.summary_message,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        optional = {
            "endedAt": self.ended_at,
            "closedEventPublishedAt": self.closed_event_published_at,
            "summaryGeneratedAt": self.summary_generated_at,
            "summaryDeliveryClaimedAt": self.summary_delivery_claimed_at,
            "summaryDeliveredAt": self.summary_delivered_at,
        }
        data.update({key: value for key, value in optional.items() if value is not None})
        return data


@dataclass(slots=True)
class ParticipantInterval:
    id: str = ""
    session_id: str = ""
    guild_id: str = ""
    channel_id: str = ""
    user_id: str = ""
    user_name: str = ""
    joined_at: datetime | None = None
    left_at: datetime | None = None
    duration_ms: int = 0
    active: bool = False

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "ParticipantInterval | None":
        if data is None:
            return None
        return cls(
            id=data.get("_id") or data.get("id", ""),
            session_id=data.get("sessionId", ""),
            guild_id=data.get("guildId", ""),
            channel_id=data.get("channelId", ""),
            user_id=data.get("userId", ""),
            user_name=data.get("userName", ""),
            joined_at=parse_datetime(data.get("joinedAt")),
            left_at=parse_datetime(data.get("leftAt")),
            duration_ms=int(data.get("durationMs", 0) or 0),
            active=bool(data.get("active", False)),
        )

    def to_mongo(self) -> dict[str, Any]:
        data = {
            "_id": self.id,
            "sessionId": self.session_id,
            "guildId": self.guild_id,
            "channelId": self.channel_id,
            "userId": self.user_id,
            "userName": self.user_name,
            "joinedAt": self.joined_at,
            "durationMs": self.duration_ms,
            "active": self.active,
        }
        if self.left_at is not None:
            data["leftAt"] = self.left_at
        return data


@dataclass(slots=True)
class ParticipantSummary:
    user_id: str
    user_name: str = ""
    intervals: int = 0
    total_time: timedelta = field(default_factory=timedelta)


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    guild_id: str
    channel_id: str
    unique_users: int
    total_duration: timedelta
    ended_by_user_id: str
    participants: list[ParticipantSummary]


def build_session_summary(
    session: Session,
    participants: list[ParticipantInterval],
    ended_by_user_id: str,
) -> SessionSummary:
    by_user: dict[str, ParticipantSummary] = {}
    unique: set[str] = set()
    for participant in participants:
        unique.add(participant.user_id)
        summary = by_user.get(participant.user_id)
        if summary is None:
            summary = ParticipantSummary(participant.user_id, participant.user_name)
            by_user[participant.user_id] = summary
        summary.intervals += 1
        duration_ms = participant.duration_ms
        if duration_ms == 0 and participant.left_at is not None and participant.joined_at is not None:
            duration_ms = int((participant.left_at - participant.joined_at).total_seconds() * 1000)
        if duration_ms < 0:
            duration_ms = 0
        summary.total_time += timedelta(milliseconds=duration_ms)

    items = list(by_user.values())
    items.sort(key=lambda item: (-item.total_time.total_seconds(), item.user_name))

    total_duration = timedelta()
    if session.started_at is not None and session.ended_at is not None:
        total_duration = positive_delta(session.ended_at - session.started_at)

    return SessionSummary(
        session_id=session.id,
        guild_id=session.guild_id,
        channel_id=session.channel_id,
        unique_users=len(unique),
        total_duration=total_duration,
        ended_by_user_id=ended_by_user_id,
        participants=items,
    )


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, datetime):
        return datetime_to_json(value)
    if isinstance(value, timedelta):
        return int(value.total_seconds() * 1000)
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    return value

