from __future__ import annotations

from dataclasses import dataclass, field, replace
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

INVITE_TYPE_REGULAR = "regular"
INVITE_TYPE_VANITY = "vanity"

INVITE_ATTRIBUTION_STATUS_EXACT = "exact"
INVITE_ATTRIBUTION_STATUS_AMBIGUOUS = "ambiguous"
INVITE_ATTRIBUTION_STATUS_UNKNOWN = "unknown"

INVITE_SOURCE_SNAPSHOT = "snapshot"
INVITE_SOURCE_LIVE_EVENT = "live_event"
INVITE_SOURCE_AUDIT_LOG = "audit_log"
INVITE_SOURCE_LIVE_DIFF = "live_diff"


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


def _clean_codes(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values or []:
        code = _clean(raw)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def invite_catalog_id(guild_id: str, code: str) -> str:
    guild_id = _clean(guild_id)
    code = _clean(code)
    if not guild_id or not code:
        return ""
    return f"{guild_id}:{code}"


def member_join_state_id(guild_id: str, user_id: str) -> str:
    guild_id = _clean(guild_id)
    user_id = _clean(user_id)
    if not guild_id or not user_id:
        return ""
    return f"{guild_id}:{user_id}"


def member_join_attribution_id(guild_id: str, user_id: str, joined_at: datetime | None) -> str:
    guild_id = _clean(guild_id)
    user_id = _clean(user_id)
    joined_at = ensure_utc(joined_at)
    if not guild_id or not user_id or joined_at is None:
        return ""
    return f"{guild_id}:{user_id}:{datetime_to_json(joined_at)}"


def member_role_state_id(guild_id: str, user_id: str) -> str:
    guild_id = _clean(guild_id)
    user_id = _clean(user_id)
    if not guild_id or not user_id:
        return ""
    return f"{guild_id}:{user_id}"


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
    soundboard_enforcement_enabled: bool = False
    managed_voice_channel_id: str = ""
    managed_voice_connected_at: datetime | None = None

    def __post_init__(self) -> None:
        self.guild_id = _clean(self.guild_id)
        self.tracking_mode = normalize_tracking_mode(self.tracking_mode)
        self.tracked_channel_ids = clean_channel_ids(self.tracked_channel_ids)
        self.summary_channel_id = _clean(self.summary_channel_id)
        self.fallback_summary_channel_id = _clean(self.fallback_summary_channel_id)
        self.auto_role_id = _clean(self.auto_role_id)
        self.auto_unmute_user_ids = clean_channel_ids(self.auto_unmute_user_ids)
        self.soundboard_enforcement_enabled = bool(self.soundboard_enforcement_enabled)
        self.managed_voice_channel_id = _clean(self.managed_voice_channel_id)
        self.managed_voice_connected_at = ensure_utc(self.managed_voice_connected_at)
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

    def canonical_for_voice_tracking(self) -> "GuildSettings":
        if self.tracking_mode == GUILD_TRACKING_MODE_ALL and not self.tracked_channel_ids:
            return self
        return replace(self, tracking_mode=GUILD_TRACKING_MODE_ALL, tracked_channel_ids=[])

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
            soundboard_enforcement_enabled=bool(data.get("soundboardEnforcementEnabled", False)),
            managed_voice_channel_id=data.get("managedVoiceChannelId", ""),
            managed_voice_connected_at=parse_datetime(data.get("managedVoiceConnectedAt")),
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
    soundboard_enforcement_enabled: bool = False,
    managed_voice_channel_id: str = "",
    managed_voice_connected_at: datetime | None = None,
) -> GuildSettings:
    return GuildSettings(
        guild_id,
        tracking_mode,
        tracked_channel_ids or [],
        summary_channel_id,
        fallback_summary_channel_id=fallback_summary_channel_id,
        auto_role_id=auto_role_id,
        auto_unmute_user_ids=auto_unmute_user_ids or [],
        soundboard_enforcement_enabled=soundboard_enforcement_enabled,
        managed_voice_channel_id=managed_voice_channel_id,
        managed_voice_connected_at=managed_voice_connected_at,
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
class InviteSnapshotEntry:
    code: str = ""
    uses: int = 0
    url: str = ""
    channel_id: str = ""
    inviter_user_id: str = ""
    inviter_name: str = ""
    invite_type: str = INVITE_TYPE_REGULAR

    def __post_init__(self) -> None:
        self.code = _clean(self.code)
        self.uses = max(0, int(self.uses or 0))
        self.url = _clean(self.url)
        self.channel_id = _clean(self.channel_id)
        self.inviter_user_id = _clean(self.inviter_user_id)
        self.inviter_name = _clean(self.inviter_name)
        invite_type = _clean(self.invite_type).lower()
        self.invite_type = invite_type if invite_type in {INVITE_TYPE_REGULAR, INVITE_TYPE_VANITY} else INVITE_TYPE_REGULAR

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "InviteSnapshotEntry | None":
        if data is None:
            return None
        return cls(
            code=data.get("code", ""),
            uses=int(data.get("uses", 0) or 0),
            url=data.get("url", ""),
            channel_id=data.get("channelId", ""),
            inviter_user_id=data.get("inviterUserId", ""),
            inviter_name=data.get("inviterName", ""),
            invite_type=data.get("inviteType", INVITE_TYPE_REGULAR),
        )

    def to_mongo(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "uses": self.uses,
            "url": self.url,
            "channelId": self.channel_id,
            "inviterUserId": self.inviter_user_id,
            "inviterName": self.inviter_name,
            "inviteType": self.invite_type,
        }


@dataclass(slots=True)
class GuildInviteSnapshot:
    guild_id: str = ""
    captured_at: datetime | None = None
    invites: list[InviteSnapshotEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.guild_id = _clean(self.guild_id)
        self.captured_at = ensure_utc(self.captured_at)
        normalized: list[InviteSnapshotEntry] = []
        for invite in self.invites:
            if isinstance(invite, InviteSnapshotEntry):
                item = invite
            else:
                item = InviteSnapshotEntry.from_mongo(invite)
            if item is not None and item.code:
                normalized.append(item)
        self.invites = normalized

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "GuildInviteSnapshot | None":
        if data is None:
            return None
        return cls(
            guild_id=data.get("_id") or data.get("guildId", ""),
            captured_at=parse_datetime(data.get("capturedAt")),
            invites=[item for item in (InviteSnapshotEntry.from_mongo(raw) for raw in data.get("invites") or []) if item is not None],
        )

    def to_mongo(self) -> dict[str, Any]:
        return {
            "_id": self.guild_id,
            "guildId": self.guild_id,
            "capturedAt": self.captured_at,
            "invites": [invite.to_mongo() for invite in self.invites],
        }


@dataclass(slots=True)
class InviteCatalogEntry:
    id: str = ""
    guild_id: str = ""
    code: str = ""
    url: str = ""
    channel_id: str = ""
    invite_type: str = INVITE_TYPE_REGULAR
    created_by_user_id: str = ""
    created_by_name: str = ""
    created_at: datetime | None = None
    deleted_at: datetime | None = None
    last_seen_at: datetime | None = None
    source: str = ""

    def __post_init__(self) -> None:
        self.guild_id = _clean(self.guild_id)
        self.code = _clean(self.code)
        if not self.id:
            self.id = invite_catalog_id(self.guild_id, self.code)
        self.id = _clean(self.id)
        self.url = _clean(self.url)
        self.channel_id = _clean(self.channel_id)
        invite_type = _clean(self.invite_type).lower()
        self.invite_type = invite_type if invite_type in {INVITE_TYPE_REGULAR, INVITE_TYPE_VANITY} else INVITE_TYPE_REGULAR
        self.created_by_user_id = _clean(self.created_by_user_id)
        self.created_by_name = _clean(self.created_by_name)
        self.created_at = ensure_utc(self.created_at)
        self.deleted_at = ensure_utc(self.deleted_at)
        self.last_seen_at = ensure_utc(self.last_seen_at)
        self.source = _clean(self.source)

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "InviteCatalogEntry | None":
        if data is None:
            return None
        return cls(
            id=data.get("_id") or data.get("id", ""),
            guild_id=data.get("guildId", ""),
            code=data.get("code", ""),
            url=data.get("url", ""),
            channel_id=data.get("channelId", ""),
            invite_type=data.get("inviteType", INVITE_TYPE_REGULAR),
            created_by_user_id=data.get("createdByUserId", ""),
            created_by_name=data.get("createdByName", ""),
            created_at=parse_datetime(data.get("createdAt")),
            deleted_at=parse_datetime(data.get("deletedAt")),
            last_seen_at=parse_datetime(data.get("lastSeenAt")),
            source=data.get("source", ""),
        )

    def to_mongo(self) -> dict[str, Any]:
        data = {
            "_id": self.id,
            "guildId": self.guild_id,
            "code": self.code,
            "url": self.url,
            "channelId": self.channel_id,
            "inviteType": self.invite_type,
            "createdByUserId": self.created_by_user_id,
            "createdByName": self.created_by_name,
            "source": self.source,
        }
        optional = {
            "createdAt": self.created_at,
            "deletedAt": self.deleted_at,
            "lastSeenAt": self.last_seen_at,
        }
        data.update({key: value for key, value in optional.items() if value is not None})
        return data


@dataclass(slots=True)
class MemberJoinAttribution:
    id: str = ""
    guild_id: str = ""
    user_id: str = ""
    joined_at: datetime | None = None
    invite_code: str = ""
    invite_url: str = ""
    invite_type: str = INVITE_TYPE_REGULAR
    inviter_user_id: str = ""
    inviter_name: str = ""
    attribution_status: str = INVITE_ATTRIBUTION_STATUS_UNKNOWN
    candidate_codes: list[str] = field(default_factory=list)
    source: str = INVITE_SOURCE_LIVE_DIFF
    snapshot_captured_at: datetime | None = None
    created_at: datetime | None = None
    internal_reason: str = ""

    def __post_init__(self) -> None:
        self.guild_id = _clean(self.guild_id)
        self.user_id = _clean(self.user_id)
        self.joined_at = ensure_utc(self.joined_at)
        if not self.id:
            self.id = member_join_attribution_id(self.guild_id, self.user_id, self.joined_at)
        self.id = _clean(self.id)
        self.invite_code = _clean(self.invite_code)
        self.invite_url = _clean(self.invite_url)
        invite_type = _clean(self.invite_type).lower()
        self.invite_type = invite_type if invite_type in {INVITE_TYPE_REGULAR, INVITE_TYPE_VANITY} else INVITE_TYPE_REGULAR
        self.inviter_user_id = _clean(self.inviter_user_id)
        self.inviter_name = _clean(self.inviter_name)
        status = _clean(self.attribution_status).lower()
        if status not in {
            INVITE_ATTRIBUTION_STATUS_EXACT,
            INVITE_ATTRIBUTION_STATUS_AMBIGUOUS,
            INVITE_ATTRIBUTION_STATUS_UNKNOWN,
        }:
            status = INVITE_ATTRIBUTION_STATUS_UNKNOWN
        self.attribution_status = status
        self.candidate_codes = _clean_codes(self.candidate_codes)
        self.source = _clean(self.source) or INVITE_SOURCE_LIVE_DIFF
        self.snapshot_captured_at = ensure_utc(self.snapshot_captured_at)
        self.created_at = ensure_utc(self.created_at)
        self.internal_reason = _clean(self.internal_reason)

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "MemberJoinAttribution | None":
        if data is None:
            return None
        return cls(
            id=data.get("_id") or data.get("id", ""),
            guild_id=data.get("guildId", ""),
            user_id=data.get("userId", ""),
            joined_at=parse_datetime(data.get("joinedAt")),
            invite_code=data.get("inviteCode", ""),
            invite_url=data.get("inviteUrl", ""),
            invite_type=data.get("inviteType", INVITE_TYPE_REGULAR),
            inviter_user_id=data.get("inviterUserId", ""),
            inviter_name=data.get("inviterName", ""),
            attribution_status=data.get("attributionStatus", INVITE_ATTRIBUTION_STATUS_UNKNOWN),
            candidate_codes=list(data.get("candidateCodes") or []),
            source=data.get("source", INVITE_SOURCE_LIVE_DIFF),
            snapshot_captured_at=parse_datetime(data.get("snapshotCapturedAt")),
            created_at=parse_datetime(data.get("createdAt")),
            internal_reason=data.get("internalReason", ""),
        )

    def to_mongo(self) -> dict[str, Any]:
        data = {
            "_id": self.id,
            "guildId": self.guild_id,
            "userId": self.user_id,
            "joinedAt": self.joined_at,
            "inviteCode": self.invite_code,
            "inviteUrl": self.invite_url,
            "inviteType": self.invite_type,
            "inviterUserId": self.inviter_user_id,
            "inviterName": self.inviter_name,
            "attributionStatus": self.attribution_status,
            "candidateCodes": list(self.candidate_codes),
            "source": self.source,
        }
        optional = {
            "snapshotCapturedAt": self.snapshot_captured_at,
            "createdAt": self.created_at,
            "internalReason": self.internal_reason or None,
        }
        data.update({key: value for key, value in optional.items() if value is not None})
        return data


@dataclass(slots=True)
class MemberJoinState:
    id: str = ""
    guild_id: str = ""
    user_id: str = ""
    latest_join_attribution_id: str = ""
    joined_at: datetime | None = None
    invite_code: str = ""
    invite_url: str = ""
    invite_type: str = INVITE_TYPE_REGULAR
    inviter_user_id: str = ""
    inviter_name: str = ""
    attribution_status: str = INVITE_ATTRIBUTION_STATUS_UNKNOWN
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        self.guild_id = _clean(self.guild_id)
        self.user_id = _clean(self.user_id)
        if not self.id:
            self.id = member_join_state_id(self.guild_id, self.user_id)
        self.id = _clean(self.id)
        self.latest_join_attribution_id = _clean(self.latest_join_attribution_id)
        self.joined_at = ensure_utc(self.joined_at)
        self.invite_code = _clean(self.invite_code)
        self.invite_url = _clean(self.invite_url)
        invite_type = _clean(self.invite_type).lower()
        self.invite_type = invite_type if invite_type in {INVITE_TYPE_REGULAR, INVITE_TYPE_VANITY} else INVITE_TYPE_REGULAR
        self.inviter_user_id = _clean(self.inviter_user_id)
        self.inviter_name = _clean(self.inviter_name)
        status = _clean(self.attribution_status).lower()
        if status not in {
            INVITE_ATTRIBUTION_STATUS_EXACT,
            INVITE_ATTRIBUTION_STATUS_AMBIGUOUS,
            INVITE_ATTRIBUTION_STATUS_UNKNOWN,
        }:
            status = INVITE_ATTRIBUTION_STATUS_UNKNOWN
        self.attribution_status = status
        self.updated_at = ensure_utc(self.updated_at)

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "MemberJoinState | None":
        if data is None:
            return None
        return cls(
            id=data.get("_id") or data.get("id", ""),
            guild_id=data.get("guildId", ""),
            user_id=data.get("userId", ""),
            latest_join_attribution_id=data.get("latestJoinAttributionId", ""),
            joined_at=parse_datetime(data.get("joinedAt")),
            invite_code=data.get("inviteCode", ""),
            invite_url=data.get("inviteUrl", ""),
            invite_type=data.get("inviteType", INVITE_TYPE_REGULAR),
            inviter_user_id=data.get("inviterUserId", ""),
            inviter_name=data.get("inviterName", ""),
            attribution_status=data.get("attributionStatus", INVITE_ATTRIBUTION_STATUS_UNKNOWN),
            updated_at=parse_datetime(data.get("updatedAt")),
        )

    def to_mongo(self) -> dict[str, Any]:
        data = {
            "_id": self.id,
            "guildId": self.guild_id,
            "userId": self.user_id,
            "latestJoinAttributionId": self.latest_join_attribution_id,
            "joinedAt": self.joined_at,
            "inviteCode": self.invite_code,
            "inviteUrl": self.invite_url,
            "inviteType": self.invite_type,
            "inviterUserId": self.inviter_user_id,
            "inviterName": self.inviter_name,
            "attributionStatus": self.attribution_status,
        }
        if self.updated_at is not None:
            data["updatedAt"] = self.updated_at
        return data


@dataclass(slots=True)
class MemberRoleState:
    id: str = ""
    guild_id: str = ""
    user_id: str = ""
    role_ids: list[str] = field(default_factory=list)
    last_seen_at: datetime | None = None
    updated_at: datetime | None = None
    last_restored_at: datetime | None = None
    pending_restore: bool = False

    def __post_init__(self) -> None:
        self.guild_id = _clean(self.guild_id)
        self.user_id = _clean(self.user_id)
        if not self.id:
            self.id = member_role_state_id(self.guild_id, self.user_id)
        self.id = _clean(self.id)
        self.role_ids = clean_channel_ids(self.role_ids)
        self.last_seen_at = ensure_utc(self.last_seen_at)
        self.updated_at = ensure_utc(self.updated_at)
        self.last_restored_at = ensure_utc(self.last_restored_at)
        self.pending_restore = bool(self.pending_restore)

    @classmethod
    def from_mongo(cls, data: dict[str, Any] | None) -> "MemberRoleState | None":
        if data is None:
            return None
        return cls(
            id=data.get("_id") or data.get("id", ""),
            guild_id=data.get("guildId", ""),
            user_id=data.get("userId", ""),
            role_ids=list(data.get("roleIds") or data.get("roles") or []),
            last_seen_at=parse_datetime(data.get("lastSeenAt")),
            updated_at=parse_datetime(data.get("updatedAt")),
            last_restored_at=parse_datetime(data.get("lastRestoredAt")),
            pending_restore=bool(data.get("pendingRestore", False)),
        )

    def to_mongo(self) -> dict[str, Any]:
        data = {
            "_id": self.id,
            "guildId": self.guild_id,
            "userId": self.user_id,
            "roleIds": list(self.role_ids),
            "pendingRestore": bool(self.pending_restore),
        }
        optional = {
            "lastSeenAt": self.last_seen_at,
            "updatedAt": self.updated_at,
            "lastRestoredAt": self.last_restored_at,
        }
        data.update({key: value for key, value in optional.items() if value is not None})
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
