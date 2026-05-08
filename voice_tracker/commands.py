from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
import math
import logging

from . import domain
from .discord_models import (
    ApplicationCommandInteractionDataOption,
    ApplicationCommandOption,
    ApplicationCommandOptionChoice,
    CHANNEL_TYPE_GUILD_STAGE_VOICE,
    CHANNEL_TYPE_GUILD_TEXT,
    CHANNEL_TYPE_GUILD_VOICE,
    INTERACTION_APPLICATION_COMMAND,
    InteractionCreate,
    InteractionResponse,
    InteractionResponseData,
    MessageEmbed,
    OPTION_TYPE_CHANNEL,
    OPTION_TYPE_INTEGER,
    OPTION_TYPE_STRING,
    OPTION_TYPE_SUB_COMMAND,
    OPTION_TYPE_SUB_COMMAND_GROUP,
    PERMISSION_ADMINISTRATOR,
    PERMISSION_MANAGE_GUILD,
)
from .timeutil import discord_timestamp, hms_duration

logger = logging.getLogger(__name__)

COMMAND_ACCESS_ADMIN_ONLY = "ADMIN_ONLY"
COMMAND_ACCESS_ALL_USER = "ALL_USER"

SETTINGS_COMMAND_NAME = "settings"
CONNECT_COMMAND_NAME = "connect"
DISCONNECT_COMMAND_NAME = "disconnect"
JUMP_COMMAND_NAME = "jump"
INSPECT_COMMAND_NAME = "inspect"
AUTOROLE_COMMAND_NAME = "autorole"
UNMUTE_COMMAND_NAME = "unmute"
DASHBOARD_COMMAND_NAME = "dashboard"
USERINFO_COMMAND_NAME = "userinfo"
STATUS_COMMAND_NAME = "status"

SETTINGS_ACTIVITY_CHANNEL_SET_COMMAND = "activity-channel-set"
SETTINGS_ACTIVITY_CHANNEL_CLEAR_COMMAND = "activity-channel-clear"
SETTINGS_ACTIVITY_MODE_COMMAND = "activity"

VOICE_COMMAND_NAMES = {
    SETTINGS_COMMAND_NAME,
    CONNECT_COMMAND_NAME,
    DISCONNECT_COMMAND_NAME,
    JUMP_COMMAND_NAME,
    INSPECT_COMMAND_NAME,
    AUTOROLE_COMMAND_NAME,
    UNMUTE_COMMAND_NAME,
    DASHBOARD_COMMAND_NAME,
    USERINFO_COMMAND_NAME,
    STATUS_COMMAND_NAME,
}
INSPECT_HISTORY_ALL_COMMAND = "history.all"
INSPECT_HISTORY_PICK_COMMAND = "history.pick"
INSPECT_ACTIVE_ALL_COMMAND = "active.all"
INSPECT_ACTIVE_CHANNEL_COMMAND = "active.channel"
MAX_CLOSED_HISTORY_ITEMS = 10
OPTION_TYPE_ROLE = 8
OPTION_TYPE_USER = 6


@dataclass(slots=True)
class CommandDefinition:
    name: str
    description: str = ""
    options: list[ApplicationCommandOption] = field(default_factory=list)
    default_member_permissions: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.options:
            payload["options"] = [option.to_dict() for option in self.options]
        if self.default_member_permissions is not None:
            payload["default_member_permissions"] = self.default_member_permissions
        return payload


ApplicationCommand = CommandDefinition


@dataclass(slots=True)
class CommandPolicy:
    root: str
    command: str
    access_class: str
    default_member_permissions: int | None = None
    handler_name: str = ""
    ephemeral_default: bool = True


COMMAND_POLICIES: dict[tuple[str, str], CommandPolicy] = {
    (SETTINGS_COMMAND_NAME, "show"): CommandPolicy(SETTINGS_COMMAND_NAME, "show", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_settings_command"),
    (SETTINGS_COMMAND_NAME, "soundboard"): CommandPolicy(SETTINGS_COMMAND_NAME, "soundboard", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_settings_command"),
    (SETTINGS_COMMAND_NAME, "summary-set"): CommandPolicy(SETTINGS_COMMAND_NAME, "summary-set", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_settings_command"),
    (SETTINGS_COMMAND_NAME, "summary-clear"): CommandPolicy(SETTINGS_COMMAND_NAME, "summary-clear", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_settings_command"),
    (SETTINGS_COMMAND_NAME, SETTINGS_ACTIVITY_CHANNEL_SET_COMMAND): CommandPolicy(SETTINGS_COMMAND_NAME, SETTINGS_ACTIVITY_CHANNEL_SET_COMMAND, COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_settings_command"),
    (SETTINGS_COMMAND_NAME, SETTINGS_ACTIVITY_CHANNEL_CLEAR_COMMAND): CommandPolicy(SETTINGS_COMMAND_NAME, SETTINGS_ACTIVITY_CHANNEL_CLEAR_COMMAND, COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_settings_command"),
    (SETTINGS_COMMAND_NAME, SETTINGS_ACTIVITY_MODE_COMMAND): CommandPolicy(SETTINGS_COMMAND_NAME, SETTINGS_ACTIVITY_MODE_COMMAND, COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_settings_command"),
    (CONNECT_COMMAND_NAME, ""): CommandPolicy(CONNECT_COMMAND_NAME, "", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_connect_command"),
    (DISCONNECT_COMMAND_NAME, ""): CommandPolicy(DISCONNECT_COMMAND_NAME, "", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_disconnect_command"),
    (JUMP_COMMAND_NAME, ""): CommandPolicy(JUMP_COMMAND_NAME, "", COMMAND_ACCESS_ALL_USER, None, "handle_jump_command"),
    (INSPECT_COMMAND_NAME, ""): CommandPolicy(INSPECT_COMMAND_NAME, "", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_inspect_command"),
    (INSPECT_COMMAND_NAME, INSPECT_ACTIVE_ALL_COMMAND): CommandPolicy(INSPECT_COMMAND_NAME, INSPECT_ACTIVE_ALL_COMMAND, COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_inspect_command"),
    (INSPECT_COMMAND_NAME, INSPECT_ACTIVE_CHANNEL_COMMAND): CommandPolicy(INSPECT_COMMAND_NAME, INSPECT_ACTIVE_CHANNEL_COMMAND, COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_inspect_command"),
    (INSPECT_COMMAND_NAME, INSPECT_HISTORY_ALL_COMMAND): CommandPolicy(INSPECT_COMMAND_NAME, INSPECT_HISTORY_ALL_COMMAND, COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_inspect_command"),
    (INSPECT_COMMAND_NAME, INSPECT_HISTORY_PICK_COMMAND): CommandPolicy(INSPECT_COMMAND_NAME, INSPECT_HISTORY_PICK_COMMAND, COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_inspect_command"),
    (AUTOROLE_COMMAND_NAME, ""): CommandPolicy(AUTOROLE_COMMAND_NAME, "", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_autorole_command"),
    (UNMUTE_COMMAND_NAME, "add"): CommandPolicy(UNMUTE_COMMAND_NAME, "add", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_unmute_command"),
    (UNMUTE_COMMAND_NAME, "remove"): CommandPolicy(UNMUTE_COMMAND_NAME, "remove", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_unmute_command"),
    (UNMUTE_COMMAND_NAME, "list"): CommandPolicy(UNMUTE_COMMAND_NAME, "list", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_unmute_command"),
    (DASHBOARD_COMMAND_NAME, ""): CommandPolicy(DASHBOARD_COMMAND_NAME, "", COMMAND_ACCESS_ALL_USER, None, "handle_dashboard_command"),
    (USERINFO_COMMAND_NAME, ""): CommandPolicy(USERINFO_COMMAND_NAME, "", COMMAND_ACCESS_ALL_USER, None, "handle_userinfo_command"),
    (STATUS_COMMAND_NAME, ""): CommandPolicy(STATUS_COMMAND_NAME, "", COMMAND_ACCESS_ADMIN_ONLY, PERMISSION_ADMINISTRATOR, "handle_status_command"),
}

COMMAND_ROUTE_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    (SETTINGS_COMMAND_NAME, ""): (SETTINGS_COMMAND_NAME, "show"),
    (CONNECT_COMMAND_NAME, "channel"): (CONNECT_COMMAND_NAME, ""),
    (JUMP_COMMAND_NAME, "channel"): (JUMP_COMMAND_NAME, ""),
    (INSPECT_COMMAND_NAME, "channel"): (INSPECT_COMMAND_NAME, ""),
    (AUTOROLE_COMMAND_NAME, "role"): (AUTOROLE_COMMAND_NAME, ""),
    (USERINFO_COMMAND_NAME, "user"): (USERINFO_COMMAND_NAME, ""),
}


class Repository(Protocol):
    def get_guild_settings(self, ctx: Any, guild_id: str) -> domain.GuildSettings | None: ...

    def upsert_guild_settings(self, ctx: Any, settings: domain.GuildSettings) -> None: ...

    def list_active_sessions_by_guild(self, ctx: Any, guild_id: str) -> list[domain.Session]: ...

    def find_active_session(self, ctx: Any, guild_id: str, channel_id: str) -> domain.Session | None: ...

    def list_active_participants_by_guild_session(
        self, ctx: Any, guild_id: str, session_id: str
    ) -> list[domain.ParticipantInterval]: ...

    def list_closed_sessions_by_guild_channel(
        self, ctx: Any, guild_id: str, channel_id: str, limit: int
    ) -> list[domain.Session]: ...

    def list_participants_by_guild_channel_session(
        self, ctx: Any, guild_id: str, channel_id: str, session_id: str
    ) -> list[domain.ParticipantInterval]: ...


@dataclass(slots=True)
class SessionParticipantView:
    user_id: str
    user_name: str
    joined_at: datetime
    active_for: timedelta


@dataclass(slots=True)
class ActiveSessionView:
    session: domain.Session
    participants: list[SessionParticipantView] = field(default_factory=list)
    active_for: timedelta = field(default_factory=timedelta)
    count: int = 0


@dataclass(slots=True)
class VoiceTotalView:
    user_id: str
    user_name: str
    total_for: timedelta
    role_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemberProfileView:
    user_id: str
    user_name: str
    total_for: timedelta
    roles: list[str] = field(default_factory=list)
    nickname: str = ""
    nickname_history: list[str] = field(default_factory=list)
    invite_code: str = ""
    invite_url: str = ""
    inviter_user_id: str = ""
    inviter_name: str = ""
    attribution_status: str = ""


@dataclass(slots=True)
class Service:
    repo: Repository | None = None

    def get_guild_settings(self, ctx: Any, guild_id: str) -> domain.GuildSettings:
        if self.repo is None:
            return domain.new_guild_settings(guild_id, domain.GUILD_TRACKING_MODE_ALL, None, "")
        settings = self.repo.get_guild_settings(ctx, guild_id)
        if settings is None:
            return domain.new_guild_settings(guild_id, domain.GUILD_TRACKING_MODE_ALL, None, "")
        return settings

    def set_tracking_mode(self, ctx: Any, guild_id: str, mode: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        settings.tracking_mode = domain.normalize_tracking_mode(mode)
        self._save(ctx, settings)
        return settings

    def set_tracked_channel_ids(self, ctx: Any, guild_id: str, ids: list[str]) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        settings.tracked_channel_ids = domain.clean_channel_ids(ids)
        settings.tracking_mode = (
            domain.GUILD_TRACKING_MODE_NONE
            if len(settings.tracked_channel_ids) == 0
            else domain.GUILD_TRACKING_MODE_SPECIFIC
        )
        self._save(ctx, settings)
        return settings

    def add_tracked_channel(self, ctx: Any, guild_id: str, channel_id: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        current_mode = domain.normalize_tracking_mode(settings.tracking_mode)
        channel_id = (channel_id or "").strip()
        if not channel_id:
            raise ValueError("channel id is required")
        settings.tracked_channel_ids = domain.clean_channel_ids([*settings.tracked_channel_ids, channel_id])
        if current_mode == domain.GUILD_TRACKING_MODE_ALL:
            settings.tracking_mode = domain.GUILD_TRACKING_MODE_ALL
        elif len(settings.tracked_channel_ids) == 0:
            settings.tracking_mode = domain.GUILD_TRACKING_MODE_NONE
        else:
            settings.tracking_mode = domain.GUILD_TRACKING_MODE_SPECIFIC
        self._save(ctx, settings)
        return settings

    def remove_tracked_channel(self, ctx: Any, guild_id: str, channel_id: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        channel_id = (channel_id or "").strip()
        if not channel_id:
            raise ValueError("channel id is required")
        settings.tracked_channel_ids = _remove_channel_id(settings.tracked_channel_ids, channel_id)
        if domain.normalize_tracking_mode(settings.tracking_mode) != domain.GUILD_TRACKING_MODE_ALL:
            settings.tracking_mode = (
                domain.GUILD_TRACKING_MODE_NONE
                if len(settings.tracked_channel_ids) == 0
                else domain.GUILD_TRACKING_MODE_SPECIFIC
            )
        self._save(ctx, settings)
        return settings

    def clear_tracked_channels(self, ctx: Any, guild_id: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        current_mode = domain.normalize_tracking_mode(settings.tracking_mode)
        settings.tracked_channel_ids = []
        if current_mode != domain.GUILD_TRACKING_MODE_ALL:
            settings.tracking_mode = domain.GUILD_TRACKING_MODE_NONE
        self._save(ctx, settings)
        return settings

    def list_tracked_channels(self, ctx: Any, guild_id: str) -> domain.GuildSettings:
        return self.get_guild_settings(ctx, guild_id)

    def set_summary_channel(self, ctx: Any, guild_id: str, channel_id: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        settings.summary_channel_id = (channel_id or "").strip()
        self._save(ctx, settings)
        return settings

    def clear_summary_channel(self, ctx: Any, guild_id: str) -> domain.GuildSettings:
        return self.set_summary_channel(ctx, guild_id, "")

    def set_soundboard_enforcement(self, ctx: Any, guild_id: str, enabled: bool) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        settings.soundboard_enforcement_enabled = bool(enabled)
        self._save(ctx, settings)
        return settings

    def set_activity_channel(self, ctx: Any, guild_id: str, channel_id: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        settings.activity_channel_id = (channel_id or "").strip()
        self._save(ctx, settings)
        return settings

    def clear_activity_channel(self, ctx: Any, guild_id: str) -> domain.GuildSettings:
        return self.set_activity_channel(ctx, guild_id, "")

    def set_activity_mode(self, ctx: Any, guild_id: str, mode: str) -> domain.GuildSettings:
        mode = (mode or "").strip().lower()
        if mode not in {domain.ACTIVITY_MODE_OFF, domain.ACTIVITY_MODE_MINIMAL, domain.ACTIVITY_MODE_FULL}:
            raise ValueError("mode must be one of: off, minimal, full")
        settings = self.get_guild_settings(ctx, guild_id)
        if mode == domain.ACTIVITY_MODE_OFF:
            settings.activity_event_types = []
        elif mode == domain.ACTIVITY_MODE_MINIMAL:
            settings.activity_event_types = sorted(
                {
                    domain.ACTIVITY_EVENT_MEMBER_JOIN,
                    domain.ACTIVITY_EVENT_MEMBER_LEAVE,
                    domain.ACTIVITY_EVENT_INVITE_USED,
                }
            )
        else:
            settings.activity_event_types = sorted(domain.ACTIVITY_EVENT_TYPES)
        self._save(ctx, settings)
        return settings

    def set_managed_voice_channel(self, ctx: Any, guild_id: str, channel_id: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        settings.managed_voice_channel_id = (channel_id or "").strip()
        if settings.managed_voice_channel_id == "":
            settings.managed_voice_connected_at = None
        self._save(ctx, settings)
        return settings

    def clear_managed_voice_channel(self, ctx: Any, guild_id: str) -> domain.GuildSettings:
        return self.set_managed_voice_channel(ctx, guild_id, "")

    def remember_fallback_summary_channel(self, ctx: Any, guild_id: str, channel_id: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        channel_id = (channel_id or "").strip()
        if not channel_id or settings.fallback_summary_channel_id == channel_id:
            return settings
        settings.fallback_summary_channel_id = channel_id
        self._save(ctx, settings)
        return settings

    def describe_settings(self, settings: domain.GuildSettings) -> str:
        summary_channel = settings.summary_channel_id.strip()
        fallback_channel = settings.fallback_summary_channel_id.strip()
        if summary_channel:
            summary_channel = _channel_mention(summary_channel)
        elif fallback_channel:
            summary_channel = f"{_channel_mention(fallback_channel)} (fallback)"
        else:
            summary_channel = "not set"

        autorole_id = _optional_setting(settings, "auto_role_id", _optional_setting(settings, "autoRoleId", ""))
        autorole = _role_mention(autorole_id) if autorole_id else "not set"
        auto_unmute_ids = list(getattr(settings, "auto_unmute_user_ids", []) or [])
        auto_unmute = f"{len(auto_unmute_ids)} user(s)" if auto_unmute_ids else "none"
        managed_voice_channel_id = _optional_setting(
            settings,
            "managed_voice_channel_id",
            _optional_setting(settings, "managedVoiceChannelId", ""),
        )
        managed_voice_channel = _channel_mention(managed_voice_channel_id) if managed_voice_channel_id else "not connected"
        soundboard_enforcement = "on" if bool(getattr(settings, "soundboard_enforcement_enabled", False)) else "off"
        activity_channel_id = _optional_setting(settings, "activity_channel_id", _optional_setting(settings, "activityChannelId", ""))
        activity_channel = _channel_mention(activity_channel_id) if activity_channel_id else "not set"
        raw_activity_events = getattr(settings, "activity_event_types", getattr(settings, "activityEventTypes", []))
        activity_mode = activity_mode_from_event_types(raw_activity_events)
        lines: list[str] = []
        lines.append(f"summary channel: {summary_channel}")
        lines.append(f"autorole: {autorole}")
        lines.append(f"auto-unmute: {auto_unmute}")
        lines.append(f"voice connection: {managed_voice_channel}")
        lines.append(f"soundboard enforcement: {soundboard_enforcement}")
        lines.append(f"activity channel: {activity_channel}")
        lines.append(f"activity mode: {activity_mode}")
        created_at = format_time(settings.created_at)
        if created_at != "":
            lines.append(f"created: {created_at}")
        return "\n".join(lines)

    def list_active_sessions(self, ctx: Any, guild_id: str) -> list[ActiveSessionView]:
        if self.repo is None:
            return []
        sessions = self.repo.list_active_sessions_by_guild(ctx, guild_id)
        views = [self._build_active_session_view(ctx, session.guild_id, session) for session in sessions]
        views.sort(key=lambda view: view.session.channel_id)
        views.sort(key=lambda view: _time_sort_key(view.session.started_at), reverse=True)
        return views

    def inspect_active_session(self, ctx: Any, guild_id: str, channel_id: str) -> ActiveSessionView | None:
        if self.repo is None:
            return None
        session = self.repo.find_active_session(ctx, guild_id, channel_id)
        if session is None:
            return None
        return self._build_active_session_view(ctx, guild_id, session)

    def describe_active_sessions(self, ctx: Any, guild_id: str) -> str:
        views = self.list_active_sessions(ctx, guild_id)
        if len(views) == 0:
            return "No active sessions."
        lines = ["Active sessions"]
        limit = min(len(views), 10)
        for index in range(limit):
            view = views[index]
            lines.append(f"- <#{view.session.channel_id}>: {view.count} users, running {format_duration(view.active_for)}")
        if len(views) > limit:
            lines.append(f"+{len(views) - limit} more sessions")
        return "\n".join(lines).strip()

    def describe_active_session(self, ctx: Any, guild_id: str, channel_id: str) -> str:
        view = self.inspect_active_session(ctx, guild_id, channel_id)
        if view is None:
            return "No active session in that channel."
        lines = [
            f"Channel: <#{view.session.channel_id}>",
            f"Started: {format_time(view.session.started_at)}",
            f"Running: {format_duration(view.active_for)}",
            f"Participants: {view.count}",
        ]
        if len(view.participants) == 0:
            lines.append("- no active participants")
        else:
            limit = min(len(view.participants), 10)
            for index in range(limit):
                participant = view.participants[index]
                name = participant.user_name or participant.user_id
                lines.append(f"- {name}: {format_duration(participant.active_for)} (joined {format_time(participant.joined_at)})")
            if len(view.participants) > limit:
                lines.append(f"+{len(view.participants) - limit} more participants")
        return "\n".join(lines).strip()

    def describe_closed_session_history(self, ctx: Any, guild_id: str, channel_id: str, limit: int) -> str:
        guild_id = (guild_id or "").strip()
        if not guild_id:
            raise ValueError("guild id is required")
        channel_id = (channel_id or "").strip()
        if not channel_id:
            raise ValueError("channel id is required")
        if limit < 1 or limit > MAX_CLOSED_HISTORY_ITEMS:
            raise ValueError(f"limit must be between 1 and {MAX_CLOSED_HISTORY_ITEMS}")
        if self.repo is None:
            return f"No closed sessions for <#{channel_id}>."
        sessions = self.repo.list_closed_sessions_by_guild_channel(ctx, guild_id, channel_id, limit + 1)
        filtered = [
            session
            for session in sessions
            if session.guild_id == guild_id and session.channel_id == channel_id and session.status == domain.SESSION_STATUS_CLOSED
        ]
        if len(filtered) == 0:
            return f"No closed sessions for <#{channel_id}>."
        has_more = len(filtered) > limit
        if has_more:
            filtered = filtered[:limit]
        lines = [f"Recent closed sessions for <#{channel_id}>"]
        for index, session in enumerate(filtered, start=1):
            participants = self.repo.list_participants_by_guild_channel_session(ctx, guild_id, channel_id, session.id)
            summary = domain.build_session_summary(session, participants, session.ended_by_user_id)
            lines.append(
                f"{index}. ended {format_relative_time(_time_or_zero(session.ended_at))}, duration {format_duration(summary.total_duration)}, {summary.unique_users} users"
            )
        if has_more:
            lines.append("More sessions available.")
        lines.append(f"Use /inspect history pick channel:<#{channel_id}> pick:<number> for details.")
        return "\n".join(lines).strip()

    def describe_closed_session_detail(self, ctx: Any, guild_id: str, channel_id: str, pick: int) -> str:
        guild_id = (guild_id or "").strip()
        if not guild_id:
            raise ValueError("guild id is required")
        channel_id = (channel_id or "").strip()
        if not channel_id:
            raise ValueError("channel id is required")
        if pick < 1 or pick > MAX_CLOSED_HISTORY_ITEMS:
            raise ValueError(f"pick must be between 1 and {MAX_CLOSED_HISTORY_ITEMS}")
        if self.repo is None:
            return f"No closed session #{pick} for <#{channel_id}>."
        sessions = self.repo.list_closed_sessions_by_guild_channel(ctx, guild_id, channel_id, pick)
        filtered = [
            session
            for session in sessions
            if session.guild_id == guild_id and session.channel_id == channel_id and session.status == domain.SESSION_STATUS_CLOSED
        ]
        if len(filtered) < pick:
            return f"No closed session #{pick} for <#{channel_id}>."
        session = filtered[pick - 1]
        participants = self.repo.list_participants_by_guild_channel_session(ctx, guild_id, channel_id, session.id)
        summary = domain.build_session_summary(session, participants, session.ended_by_user_id)
        lines = [
            f"Closed session for <#{channel_id}> (#{pick} most recent)",
            f"Session ID: {session.id}",
            f"Started: {format_time(session.started_at)}",
            f"Ended: {format_time(_time_or_zero(session.ended_at))}",
            f"Duration: {format_duration(summary.total_duration)}",
            f"Unique users: {summary.unique_users}",
        ]
        if session.ended_by_user_id:
            lines.append(f"Ended by: {participant_display_name(summary.participants, session.ended_by_user_id)}")
        lines.extend(["", "Participants"])
        if len(summary.participants) == 0:
            lines.append("- none")
        for participant in summary.participants:
            name = participant.user_name or participant.user_id
            lines.append(f"- {name} - {format_duration(participant.total_time)} ({interval_label(participant.intervals)})")
        return "\n".join(lines).strip()

    def _build_active_session_view(self, ctx: Any, guild_id: str, session: domain.Session) -> ActiveSessionView:
        if self.repo is None:
            participants = []
        else:
            participants = self.repo.list_active_participants_by_guild_session(ctx, guild_id, session.id)
        now = datetime.now(timezone.utc)
        started_at = session.started_at or now
        view = ActiveSessionView(session=session, active_for=now - started_at, count=len(participants))
        for participant in participants:
            joined_at = participant.joined_at or now
            view.participants.append(
                SessionParticipantView(
                    user_id=participant.user_id,
                    user_name=participant.user_name,
                    joined_at=joined_at,
                    active_for=now - joined_at,
                )
            )
        view.participants.sort(key=lambda item: (item.joined_at, item.user_name))
        return view

    def _save(self, ctx: Any, settings: domain.GuildSettings) -> None:
        if self.repo is None:
            return
        settings.guild_id = settings.guild_id.strip()
        settings.tracking_mode = domain.normalize_tracking_mode(settings.tracking_mode)
        settings.tracked_channel_ids = domain.clean_channel_ids(settings.tracked_channel_ids)
        settings.soundboard_enforcement_enabled = bool(settings.soundboard_enforcement_enabled)
        settings.managed_voice_channel_id = (settings.managed_voice_channel_id or "").strip()
        if settings.managed_voice_channel_id == "":
            settings.managed_voice_connected_at = None
        settings.invite_snapshot_sync_enabled = bool(getattr(settings, "invite_snapshot_sync_enabled", True))
        settings.invite_live_attribution_enabled = bool(getattr(settings, "invite_live_attribution_enabled", True))
        settings.invite_userinfo_enabled = bool(getattr(settings, "invite_userinfo_enabled", True))
        settings.invite_reconciliation_enabled = bool(getattr(settings, "invite_reconciliation_enabled", False))
        settings.activity_channel_id = str(getattr(settings, "activity_channel_id", "") or "").strip()
        settings.activity_event_types = domain.clean_activity_event_types(getattr(settings, "activity_event_types", []))
        self.repo.upsert_guild_settings(ctx, settings)

    def install(self, session: Any, allowed_guild_id: str, bot_admin_user_ids: list[str]) -> Any:
        def handler(ds: Any, interaction: InteractionCreate) -> None:
            if interaction.type != INTERACTION_APPLICATION_COMMAND:
                return None
            if interaction.guild_id == "":
                respond_ephemeral(ds, interaction, "This command can only be used in a server.")
                return None
            if not allowed_guild_id.strip() or interaction.guild_id != allowed_guild_id:
                return None

            data = interaction.application_command_data()
            if data.name not in VOICE_COMMAND_NAMES:
                return None

            root, command, options = parse_voice_route(data)
            if not can_use_voice_command(interaction, bot_admin_user_ids, root, command):
                respond_ephemeral(ds, interaction, "Insufficient permissions.")
                return None

            try:
                if interaction.channel_id:
                    self.remember_fallback_summary_channel(None, interaction.guild_id, interaction.channel_id)
                content = self.handle_voice_command(None, interaction, root, command, options)
            except ValueError as exc:
                content = str(exc)
            except Exception:
                logger.exception("voice command failed")
                content = "Command failed. Check service logs."
            if not content:
                content = "Done."
            respond_ephemeral(ds, interaction, content)
            return None

        if hasattr(session, "add_handler"):
            session.add_handler(handler)
        return handler

    def handle_voice_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        root: str,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        policy = command_policy(root, command)
        if policy is None:
            raise ValueError("unknown voice command")
        handler = getattr(self, policy.handler_name, None)
        if not callable(handler):
            raise ValueError("unknown voice command")
        return handler(ctx, interaction, policy.command, options)

    def handle_settings_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        if command in {"", "show"}:
            settings = self.get_guild_settings(ctx, interaction.guild_id)
            return self.describe_settings(settings)
        if command == "soundboard":
            requested_state = option_string(options, "state").lower()
            if requested_state not in {"on", "off"}:
                raise ValueError("state must be 'on' or 'off'")
            settings = self.set_soundboard_enforcement(ctx, interaction.guild_id, requested_state == "on")
            return self.describe_settings(settings)
        if command == "summary-set":
            channel_id = resolve_command_channel(interaction, options, "channel", CHANNEL_TYPE_GUILD_TEXT)
            settings = self.set_summary_channel(ctx, interaction.guild_id, channel_id)
            return self.describe_settings(settings)
        if command == "summary-clear":
            settings = self.clear_summary_channel(ctx, interaction.guild_id)
            return self.describe_settings(settings)
        if command == SETTINGS_ACTIVITY_CHANNEL_SET_COMMAND:
            channel_id = resolve_command_channel(interaction, options, "channel", CHANNEL_TYPE_GUILD_TEXT)
            settings = self.set_activity_channel(ctx, interaction.guild_id, channel_id)
            return self.describe_settings(settings)
        if command == SETTINGS_ACTIVITY_CHANNEL_CLEAR_COMMAND:
            settings = self.clear_activity_channel(ctx, interaction.guild_id)
            return self.describe_settings(settings)
        if command == SETTINGS_ACTIVITY_MODE_COMMAND:
            mode = option_string(options, "mode").lower()
            settings = self.set_activity_mode(ctx, interaction.guild_id, mode)
            return self.describe_settings(settings)
        raise ValueError("unknown settings command")

    def enforce_track_all(self, ctx: Any, guild_id: str) -> domain.GuildSettings:
        settings = self.get_guild_settings(ctx, guild_id)
        needs_save = False
        if domain.normalize_tracking_mode(settings.tracking_mode) != domain.GUILD_TRACKING_MODE_ALL:
            settings.tracking_mode = domain.GUILD_TRACKING_MODE_ALL
            needs_save = True
        if len(settings.tracked_channel_ids) > 0:
            settings.tracked_channel_ids = []
            needs_save = True
        if needs_save:
            self._save(ctx, settings)
        return settings

    def handle_inspect_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        if command in {"", "channel", INSPECT_ACTIVE_CHANNEL_COMMAND}:
            channel_id = resolve_command_channel(
                interaction, options, "channel", CHANNEL_TYPE_GUILD_VOICE, CHANNEL_TYPE_GUILD_STAGE_VOICE
            )
            return self.describe_active_session(ctx, interaction.guild_id, channel_id)
        if command == INSPECT_ACTIVE_ALL_COMMAND:
            return self.describe_active_sessions(ctx, interaction.guild_id)
        if command == INSPECT_HISTORY_ALL_COMMAND:
            channel_id = resolve_command_channel(
                interaction, options, "channel", CHANNEL_TYPE_GUILD_VOICE, CHANNEL_TYPE_GUILD_STAGE_VOICE
            )
            limit = option_int_in_range(options, "limit", 5, 1, MAX_CLOSED_HISTORY_ITEMS)
            return self.describe_closed_session_history(ctx, interaction.guild_id, channel_id, limit)
        if command == INSPECT_HISTORY_PICK_COMMAND:
            channel_id = resolve_command_channel(
                interaction, options, "channel", CHANNEL_TYPE_GUILD_VOICE, CHANNEL_TYPE_GUILD_STAGE_VOICE
            )
            pick = option_int_in_range(options, "pick", 1, 1, MAX_CLOSED_HISTORY_ITEMS)
            return self.describe_closed_session_detail(ctx, interaction.guild_id, channel_id, pick)
        raise ValueError("unknown inspect command")

    def handle_connect_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        channel_id = resolve_command_channel(
            interaction, options, "channel", CHANNEL_TYPE_GUILD_VOICE, CHANNEL_TYPE_GUILD_STAGE_VOICE
        )
        settings = self.set_managed_voice_channel(ctx, interaction.guild_id, channel_id)
        return (
            f"Managed voice channel set to <#{channel_id}>. "
            f"Soundboard enforcement is {'on' if settings.soundboard_enforcement_enabled else 'off'}."
        )

    def handle_disconnect_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        self.clear_managed_voice_channel(ctx, interaction.guild_id)
        return "Managed voice connection disabled."

    def handle_jump_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        channel_id = resolve_command_channel(
            interaction, options, "channel", CHANNEL_TYPE_GUILD_VOICE, CHANNEL_TYPE_GUILD_STAGE_VOICE
        )
        return f"Jump target validated: <#{channel_id}>"

    def handle_autorole_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        role_id = option_role_id(options, "role")
        if not role_id:
            raise ValueError("role is required")
        settings = self.get_guild_settings(ctx, interaction.guild_id)
        if hasattr(settings, "auto_role_id"):
            setattr(settings, "auto_role_id", role_id)
            self._save(ctx, settings)
        return f"Autorole configured: {_role_mention(role_id)}"

    def handle_unmute_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        if self.repo is None:
            raise ValueError("repository unavailable")
        guild_id = interaction.guild_id
        if command == "add":
            user_id = option_user_id(options, "user")
            if not user_id:
                raise ValueError("user is required")
            ids = self.repo.add_auto_unmute_user(ctx, guild_id, user_id)
            return f"Added <@{user_id}> to auto-unmute list.\n{_describe_auto_unmute_list(ids)}"
        if command == "remove":
            user_id = option_user_id(options, "user")
            if not user_id:
                raise ValueError("user is required")
            ids = self.repo.remove_auto_unmute_user(ctx, guild_id, user_id)
            return f"Removed <@{user_id}> from auto-unmute list.\n{_describe_auto_unmute_list(ids)}"
        if command == "list":
            ids = self.repo.get_auto_unmute_user_ids(ctx, guild_id)
            return _describe_auto_unmute_list(ids)
        raise ValueError("unknown unmute command")

    def handle_dashboard_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        totals = _load_voice_totals(self.repo, ctx, interaction.guild_id)
        if len(totals) == 0:
            return "No voice totals yet."
        lines = ["Voice dashboard"]
        for index, total in enumerate(totals[:10], start=1):
            lines.append(f"{index}. {total.user_name or total.user_id}: {format_duration(total.total_for)}")
        if len(totals) > 10:
            lines.append(f"+{len(totals) - 10} more users")
        return "\n".join(lines)

    def list_dashboard_totals(self, ctx: Any, guild_id: str) -> list[VoiceTotalView]:
        return _load_voice_totals(self.repo, ctx, guild_id)

    def handle_userinfo_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        user_id = option_user_id(options, "user") or _interaction_user_id(interaction)
        if not user_id:
            raise ValueError("user is required")
        profile = _load_member_profile(self.repo, ctx, interaction.guild_id, user_id)
        if profile is None:
            lines = [
                f"User: {user_id}",
                f"Total voice time: {format_duration(timedelta())}",
                *_member_profile_invite_lines(None),
            ]
            return "\n".join(lines)
        lines = [f"User: {profile.user_name or profile.user_id}", f"Total voice time: {format_duration(profile.total_for)}"]
        lines.extend(_member_profile_nickname_lines(profile))
        if len(profile.roles) > 0:
            lines.append(f"Roles: {_truncate_list(profile.roles, 10)}")
        lines.extend(_member_profile_invite_lines(profile))
        return "\n".join(lines)

    def handle_status_command(
        self,
        ctx: Any,
        interaction: InteractionCreate,
        command: str,
        options: list[ApplicationCommandInteractionDataOption],
    ) -> str:
        del ctx, interaction, command
        state = option_string(options, "state").lower().strip()
        allowed = {"online", "idle", "dnd", "invisible", "offline"}
        if state not in allowed:
            raise ValueError("state must be one of: online, idle, dnd, invisible, offline")
        return f"Bot status requested: {state}."

    def get_member_profile(self, ctx: Any, guild_id: str, user_id: str) -> MemberProfileView | None:
        return _load_member_profile(self.repo, ctx, guild_id, user_id)


def build_dashboard_page(model: Any) -> dict[str, Any]:
    rows = list(getattr(model, "rows", []) or [])
    page_rows: list[dict[str, Any]] = []
    for raw in rows[:10]:
        rank = getattr(raw, "rank", None)
        display_name = _sanitize_public_text(str(getattr(raw, "display_name", "") or ""))
        total_voice_time = str(getattr(raw, "total_voice_time", getattr(raw, "total_for", "")) or "")
        page_rows.append(
            {
                "rank": rank,
                "display_name": display_name,
                "total_voice_time": total_voice_time,
            }
        )
    return {
        "rows": page_rows,
        "page": int(getattr(model, "page", 1) or 1),
        "page_size": int(getattr(model, "page_size", 10) or 10),
    }


def build_userinfo_page(model: Any) -> dict[str, Any]:
    user = getattr(model, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    display_name = _sanitize_public_text(str(getattr(user, "display_name", user_id) or user_id))
    avatar_url = str(getattr(user, "avatar_url", "") or "")
    safe_roles: list[str] = []
    for role in list(getattr(model, "roles", []) or []):
        cleaned = _sanitize_public_text(str(role or ""))
        if cleaned == "":
            continue
        safe_roles.append(cleaned)
    return {
        "user": {
            "id": user_id,
            "display_name": display_name,
            "avatar_url": avatar_url,
        },
        "roles": safe_roles[:10],
        "total_voice_time": str(getattr(model, "total_voice_time", "0s") or "0s"),
    }


dashboard_page_builder = build_dashboard_page
userinfo_page_builder = build_userinfo_page
BuildDashboardPage = build_dashboard_page
BuildUserInfoPage = build_userinfo_page


def voice_application_commands() -> list[ApplicationCommand]:
    return [
        settings_application_command(),
        connect_application_command(),
        disconnect_application_command(),
        jump_application_command(),
        inspect_application_command(),
        autorole_application_command(),
        unmute_application_command(),
        dashboard_application_command(),
        userinfo_application_command(),
        status_application_command(),
    ]


def voice_application_command() -> ApplicationCommand:
    return settings_application_command()


def jump_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=JUMP_COMMAND_NAME,
        description="Move yourself to a visible voice channel",
        options=[_voice_channel_option("channel", "Voice channel to join")],
    )


def connect_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=CONNECT_COMMAND_NAME,
        description="Pin bot to a voice channel",
        options=[_voice_channel_option("channel", "Voice channel to manage")],
        default_member_permissions=PERMISSION_ADMINISTRATOR,
    )


def disconnect_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=DISCONNECT_COMMAND_NAME,
        description="Unpin bot and disconnect from voice",
        default_member_permissions=PERMISSION_ADMINISTRATOR,
    )


def inspect_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=INSPECT_COMMAND_NAME,
        description="Inspect one voice channel",
        options=[
            _voice_channel_option("channel", "Voice channel to inspect"),
        ],
        default_member_permissions=PERMISSION_ADMINISTRATOR,
    )


def autorole_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=AUTOROLE_COMMAND_NAME,
        description="Configure the guild autorole",
        options=[_role_option("role", "Role to assign when members join")],
        default_member_permissions=PERMISSION_ADMINISTRATOR,
    )


def unmute_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=UNMUTE_COMMAND_NAME,
        description="Manage auto-unmute user list",
        options=[
            ApplicationCommandOption(
                type=OPTION_TYPE_SUB_COMMAND,
                name="add",
                description="Add a user to the auto-unmute list",
                options=[_user_option("user", "User to auto-unmute")],
            ),
            ApplicationCommandOption(
                type=OPTION_TYPE_SUB_COMMAND,
                name="remove",
                description="Remove a user from the auto-unmute list",
                options=[_user_option("user", "User to remove from auto-unmute")],
            ),
            ApplicationCommandOption(type=OPTION_TYPE_SUB_COMMAND, name="list", description="List auto-unmute users"),
        ],
        default_member_permissions=PERMISSION_ADMINISTRATOR,
    )


def dashboard_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=DASHBOARD_COMMAND_NAME,
        description="Show voice-time leaderboard",
    )


def userinfo_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=USERINFO_COMMAND_NAME,
        description="Show a member's voice-time summary",
        options=[_user_option("user", "Member to inspect")],
    )


def status_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=STATUS_COMMAND_NAME,
        description="Set bot presence status",
        options=[_status_state_option()],
        default_member_permissions=PERMISSION_ADMINISTRATOR,
    )


def settings_application_command() -> CommandDefinition:
    return CommandDefinition(
        name=SETTINGS_COMMAND_NAME,
        description="Show or change voice tracker settings",
        options=[
            ApplicationCommandOption(type=OPTION_TYPE_SUB_COMMAND, name="show", description="Show current settings"),
            ApplicationCommandOption(
                type=OPTION_TYPE_SUB_COMMAND,
                name="soundboard",
                description="Toggle soundboard kick enforcement",
                options=[_soundboard_state_option()],
            ),
            ApplicationCommandOption(
                type=OPTION_TYPE_SUB_COMMAND,
                name="summary-set",
                description="Set the text channel for session summaries",
                options=[_text_channel_option("channel", "Summary text channel")],
            ),
            ApplicationCommandOption(
                type=OPTION_TYPE_SUB_COMMAND,
                name="summary-clear",
                description="Use the last command channel for summaries",
            ),
            ApplicationCommandOption(
                type=OPTION_TYPE_SUB_COMMAND,
                name=SETTINGS_ACTIVITY_CHANNEL_SET_COMMAND,
                description="Set activity messages channel",
                options=[_text_channel_option("channel", "Activity text channel")],
            ),
            ApplicationCommandOption(
                type=OPTION_TYPE_SUB_COMMAND,
                name=SETTINGS_ACTIVITY_CHANNEL_CLEAR_COMMAND,
                description="Disable activity message channel",
            ),
            ApplicationCommandOption(
                type=OPTION_TYPE_SUB_COMMAND,
                name=SETTINGS_ACTIVITY_MODE_COMMAND,
                description="Set activity feed mode",
                options=[_activity_mode_option()],
            ),
        ],
        default_member_permissions=PERMISSION_ADMINISTRATOR,
    )


def registered_voice_command_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for command in voice_application_commands():
        root = (command.name or "").strip()
        if root == "":
            continue
        routes.update(_registered_routes_for_command(root, command.options))
    return routes


def _registered_routes_for_command(root: str, options: list[ApplicationCommandOption]) -> set[tuple[str, str]]:
    if len(options) == 0:
        return {(root, "")}
    routes: set[tuple[str, str]] = set()
    for option in options:
        if _is_subcommand_group(option):
            for nested in list(option.options or []):
                if _is_subcommand(nested):
                    routes.add((root, f"{option.name}.{nested.name}"))
            continue
        if _is_subcommand(option):
            routes.add((root, option.name))
    if len(routes) > 0:
        return routes
    return {(root, "")}


def _voice_channel_option(name: str, description: str) -> ApplicationCommandOption:
    return ApplicationCommandOption(
        type=OPTION_TYPE_CHANNEL,
        name=name,
        description=description,
        required=True,
        channel_types=[CHANNEL_TYPE_GUILD_VOICE, CHANNEL_TYPE_GUILD_STAGE_VOICE],
    )


def _text_channel_option(name: str, description: str) -> ApplicationCommandOption:
    return ApplicationCommandOption(
        type=OPTION_TYPE_CHANNEL,
        name=name,
        description=description,
        required=True,
        channel_types=[CHANNEL_TYPE_GUILD_TEXT],
    )


def _role_option(name: str, description: str) -> ApplicationCommandOption:
    return ApplicationCommandOption(type=OPTION_TYPE_ROLE, name=name, description=description, required=True)


def _user_option(name: str, description: str) -> ApplicationCommandOption:
    return ApplicationCommandOption(type=OPTION_TYPE_USER, name=name, description=description, required=True)


def _activity_mode_option() -> ApplicationCommandOption:
    return ApplicationCommandOption(
        type=OPTION_TYPE_STRING,
        name="mode",
        description="off, minimal, or full",
        required=True,
        choices=[
            ApplicationCommandOptionChoice(name=domain.ACTIVITY_MODE_OFF, value=domain.ACTIVITY_MODE_OFF),
            ApplicationCommandOptionChoice(name=domain.ACTIVITY_MODE_MINIMAL, value=domain.ACTIVITY_MODE_MINIMAL),
            ApplicationCommandOptionChoice(name=domain.ACTIVITY_MODE_FULL, value=domain.ACTIVITY_MODE_FULL),
        ],
    )


def _soundboard_state_option() -> ApplicationCommandOption:
    return ApplicationCommandOption(
        type=OPTION_TYPE_STRING,
        name="state",
        description="Use on/off",
        required=True,
        choices=[
            ApplicationCommandOptionChoice(name="on", value="on"),
            ApplicationCommandOptionChoice(name="off", value="off"),
        ],
    )


def activity_mode_from_event_types(values: list[str] | tuple[str, ...] | None) -> str:
    selected = set(domain.clean_activity_event_types(values))
    if len(selected) == 0:
        return domain.ACTIVITY_MODE_OFF
    if selected == set(domain.ACTIVITY_EVENT_TYPES):
        return domain.ACTIVITY_MODE_FULL
    return domain.ACTIVITY_MODE_MINIMAL


def _status_state_option() -> ApplicationCommandOption:
    return ApplicationCommandOption(
        type=OPTION_TYPE_STRING,
        name="state",
        description="online, idle, dnd, invisible, or offline",
        required=True,
        choices=[
            ApplicationCommandOptionChoice(name="online", value="online"),
            ApplicationCommandOptionChoice(name="idle", value="idle"),
            ApplicationCommandOptionChoice(name="dnd", value="dnd"),
            ApplicationCommandOptionChoice(name="invisible", value="invisible"),
            ApplicationCommandOptionChoice(name="offline", value="offline"),
        ],
    )


def parse_voice_route(
    data: Any,
) -> tuple[str, str, list[ApplicationCommandInteractionDataOption]]:
    if isinstance(data, list):
        return _parse_nested_route("", data)
    name = str(getattr(data, "name", "")).strip()
    return _parse_nested_route(name, list(getattr(data, "options", []) or []))


def _parse_nested_route(
    root: str,
    options: list[ApplicationCommandInteractionDataOption],
) -> tuple[str, str, list[ApplicationCommandInteractionDataOption]]:
    if len(options) == 0:
        return root, "", []
    first = options[0]
    if not _is_subcommand_group(first):
        if _is_subcommand(first):
            return root, first.name, list(first.options)
        return root, "", list(options)
    if len(first.options) == 0:
        return root, "", []
    second = first.options[0]
    return root, f"{first.name}.{second.name}", list(second.options)


def _is_subcommand_group(option: ApplicationCommandInteractionDataOption) -> bool:
    return _option_type_value(option.type) == _option_type_value(OPTION_TYPE_SUB_COMMAND_GROUP)


def _is_subcommand(option: ApplicationCommandInteractionDataOption) -> bool:
    return _option_type_value(option.type) == _option_type_value(OPTION_TYPE_SUB_COMMAND)


def _option_type_value(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, bytes, bytearray)):
        value = value.value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="ignore")
    return {"subcommand": 1, "subcommand_group": 2}.get(str(value), value)


def can_use_voice_command(
    interaction: InteractionCreate | None, bot_admin_user_ids: list[str], root: str, command: str
) -> bool:
    policy = command_policy(root, command)
    if policy is None:
        return False
    if policy.access_class == COMMAND_ACCESS_ALL_USER:
        return True
    return has_administrator_permissions(interaction)


def canonical_command_route(root: str, command: str) -> tuple[str, str]:
    canonical = ((root or "").strip(), (command or "").strip())
    visited: set[tuple[str, str]] = set()
    while canonical in COMMAND_ROUTE_ALIASES:
        if canonical in visited:
            break
        visited.add(canonical)
        canonical = COMMAND_ROUTE_ALIASES[canonical]
    return canonical


def command_policy(root: str, command: str) -> CommandPolicy | None:
    return COMMAND_POLICIES.get(canonical_command_route(root, command))


def option_string(options: list[ApplicationCommandInteractionDataOption], name: str) -> str:
    for option in options:
        if option.name == name and isinstance(option.value, str):
            return option.value.strip()
    return ""


def option_channel_id(options: list[ApplicationCommandInteractionDataOption], name: str) -> str:
    return option_snowflake_id(options, name)


def option_role_id(options: list[ApplicationCommandInteractionDataOption], name: str) -> str:
    return option_snowflake_id(options, name)


def option_user_id(options: list[ApplicationCommandInteractionDataOption], name: str) -> str:
    return option_snowflake_id(options, name)


def option_snowflake_id(options: list[ApplicationCommandInteractionDataOption], name: str) -> str:
    for option in options:
        if option.name != name:
            continue
        return normalize_snowflake_id(option.value)
    return ""


def normalize_snowflake_id(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        if value < 0:
            return ""
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def option_int_in_range(
    options: list[ApplicationCommandInteractionDataOption], name: str, fallback: int, min_value: int, max_value: int
) -> int:
    if fallback < min_value or fallback > max_value:
        raise ValueError(f"invalid default for {name}")
    for option in options:
        if option.name != name:
            continue
        value = option.value
        if isinstance(value, bool):
            raise ValueError(f"invalid {name} value")
        if isinstance(value, int):
            if value < min_value or value > max_value:
                raise ValueError(f"{name} must be between {min_value} and {max_value}")
            return value
        if isinstance(value, float):
            if math.trunc(value) != value:
                raise ValueError(f"{name} must be a whole number")
            int_value = int(value)
            if int_value < min_value or int_value > max_value:
                raise ValueError(f"{name} must be between {min_value} and {max_value}")
            return int_value
        raise ValueError(f"invalid {name} value")
    return fallback


def resolve_command_channel(
    interaction: InteractionCreate,
    options: list[ApplicationCommandInteractionDataOption],
    name: str,
    *allowed_types: str,
) -> str:
    channel_id = option_channel_id(options, name)
    if not channel_id:
        raise ValueError("channel is required")
    resolved = interaction.application_command_data().resolved
    if resolved is None or resolved.channels is None:
        raise ValueError("channel resolution unavailable")
    channel = resolved.channels.get(channel_id)
    if channel is None:
        raise ValueError("unable to resolve channel")
    if channel.guild_id and channel.guild_id != interaction.guild_id:
        raise ValueError("channel must belong to this guild")
    if len(allowed_types) > 0 and not channel_type_allowed(channel.type, *allowed_types):
        raise ValueError("unsupported channel type")
    return channel_id


def channel_type_allowed(channel_type: str, *allowed_types: str) -> bool:
    normalized = _channel_type_value(channel_type)
    return any(normalized == _channel_type_value(allowed) for allowed in allowed_types)


def _channel_type_value(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, bytes, bytearray)):
        return value.value
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="ignore")
    return {"guild_text": 0, "guild_voice": 2, "guild_stage_voice": 13}.get(str(value), value)


def has_manage_permissions(interaction: InteractionCreate | None) -> bool:
    if interaction is None or interaction.member is None:
        return False
    permissions = interaction.member.permissions
    return bool(permissions & (PERMISSION_ADMINISTRATOR | PERMISSION_MANAGE_GUILD))


def has_administrator_permissions(interaction: InteractionCreate | None) -> bool:
    if interaction is None or interaction.member is None:
        return False
    return bool(interaction.member.permissions & PERMISSION_ADMINISTRATOR)


def respond_ephemeral(session: Any, interaction: InteractionCreate, content: str) -> InteractionResponse:
    response = InteractionResponse(
        data=InteractionResponseData(
            flags=1 << 6,
            embeds=[MessageEmbed(title="Voice Tracker", description=content, color=0x5865F2)],
        )
    )
    if hasattr(session, "interaction_respond"):
        session.interaction_respond(interaction.interaction, response)
    return response


def format_duration(value: timedelta) -> str:
    return hms_duration(value, round_seconds=True)


def format_time(value: datetime | None) -> str:
    return discord_timestamp(value)


def format_relative_time(value: datetime | None) -> str:
    return discord_timestamp(value, style="relative")


def participant_display_name(participants: list[domain.ParticipantSummary], user_id: str) -> str:
    user_id = (user_id or "").strip()
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


def _describe_auto_unmute_list(user_ids: list[str]) -> str:
    if not user_ids:
        return "Auto-unmute list is empty."
    mentions = ", ".join(f"<@{uid}>" for uid in user_ids)
    return f"Auto-unmute list ({len(user_ids)}): {mentions}"


def _remove_channel_id(ids: list[str], target: str) -> list[str]:
    return [value for value in ids if value.strip() != target]


def _channel_mentions(ids: list[str]) -> str:
    if len(ids) == 0:
        return ""
    return ", ".join(_channel_mention(channel_id) for channel_id in ids)


def _channel_mention(channel_id: str) -> str:
    return f"<#{channel_id}>" if channel_id else ""


def _time_or_zero(value: datetime | None) -> datetime:
    return value if value is not None else datetime.min.replace(tzinfo=timezone.utc)


def _time_sort_key(value: datetime | None) -> datetime:
    return value if value is not None else datetime.min.replace(tzinfo=timezone.utc)


def _optional_setting(settings: Any, name: str, default: str) -> str:
    value = getattr(settings, name, default)
    if value is None:
        return default
    return str(value).strip()


def _role_mention(role_id: str) -> str:
    role_id = (role_id or "").strip()
    if not role_id:
        return ""
    return f"<@&{role_id}>"


def _interaction_user_id(interaction: InteractionCreate) -> str:
    if interaction.member is not None and interaction.member.user is not None:
        return str(interaction.member.user.id).strip()
    if interaction.user is not None:
        return str(interaction.user.id).strip()
    return ""


def _sanitize_public_text(value: str) -> str:
    text = str(value or "")
    for token in ("@everyone", "@here"):
        text = text.replace(token, "")
    text = _strip_discord_mentions(text)
    text = " ".join(text.split())
    return text.strip()


def _strip_discord_mentions(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "<":
            end = text.find(">", index + 1)
            if end != -1 and text[index + 1 : end + 1].startswith("@"):
                index = end + 1
                continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _truncate_list(values: list[str], limit: int) -> str:
    cleaned = [str(value).strip() for value in values if str(value).strip()]
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    return ", ".join(cleaned[:limit]) + f", +{len(cleaned) - limit} more"


def _load_voice_totals(repo: Any, ctx: Any, guild_id: str) -> list[VoiceTotalView]:
    if repo is None:
        return []
    loader = getattr(repo, "list_voice_totals_by_guild", None)
    if callable(loader):
        rows = loader(ctx, guild_id)
        return [_voice_total_from_row(row) for row in rows]
    participants_loader = getattr(repo, "list_participants_by_guild", None)
    if callable(participants_loader):
        rows = participants_loader(ctx, guild_id)
        totals: dict[str, VoiceTotalView] = {}
        for row in rows:
            if getattr(row, "guild_id", "") != guild_id:
                continue
            total = totals.get(row.user_id)
            if total is None:
                total = VoiceTotalView(row.user_id, getattr(row, "user_name", "") or row.user_id, timedelta())
                totals[row.user_id] = total
            duration_ms = int(getattr(row, "duration_ms", 0) or 0)
            if duration_ms < 0:
                duration_ms = 0
            total.total_for += timedelta(milliseconds=duration_ms)
        ordered = list(totals.values())
        ordered.sort(key=lambda item: (-item.total_for.total_seconds(), item.user_name, item.user_id))
        return ordered
    return []


def _voice_total_from_row(row: Any) -> VoiceTotalView:
    if isinstance(row, VoiceTotalView):
        return row
    if isinstance(row, dict):
        return VoiceTotalView(
            user_id=str(row.get("user_id") or row.get("userId") or "").strip(),
            user_name=str(row.get("user_name") or row.get("userName") or "").strip(),
            total_for=_timedelta_from_value(row.get("total_for") or row.get("totalFor") or row.get("total_time") or row.get("totalTime") or 0),
            role_names=[str(role).strip() for role in row.get("role_names") or row.get("roleNames") or [] if str(role).strip()],
        )
    return VoiceTotalView(
        user_id=str(getattr(row, "user_id", getattr(row, "userId", ""))).strip(),
        user_name=str(getattr(row, "user_name", getattr(row, "userName", ""))).strip(),
        total_for=_timedelta_from_value(getattr(row, "total_for", getattr(row, "totalFor", getattr(row, "total_time", getattr(row, "totalTime", 0))))),
        role_names=[str(role).strip() for role in getattr(row, "role_names", getattr(row, "roleNames", [])) if str(role).strip()],
    )


def _timedelta_from_value(value: Any) -> timedelta:
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(milliseconds=int(value))
    return timedelta()


def _member_profile_invite_lines(profile: MemberProfileView | None) -> list[str]:
    attribution_status = _member_profile_attribution_status(profile)
    if attribution_status == "exact":
        invite_used = _sanitize_public_text(profile.invite_url) or "unknown"
        invite_creator = _member_profile_invite_creator(profile) or "unknown"
    else:
        invite_used = attribution_status
        invite_creator = attribution_status
    return [
        f"Invite used: {invite_used}",
        f"Invite created by: {invite_creator}",
        f"Invite attribution: {attribution_status}",
    ]


def _member_profile_nickname_lines(profile: MemberProfileView | None) -> list[str]:
    if profile is None:
        return []
    lines: list[str] = []
    nickname = _sanitize_public_text(profile.nickname)
    if nickname != "":
        lines.append(f"Nickname: {nickname}")
    history = [
        cleaned
        for cleaned in (_sanitize_public_text(value) for value in list(profile.nickname_history or []))
        if cleaned != ""
    ]
    if len(history) > 0:
        lines.append(f"Past nicknames: {_truncate_list(history, 10)}")
    return lines


def _member_profile_attribution_status(profile: MemberProfileView | None) -> str:
    status = str(getattr(profile, "attribution_status", "") or "").strip().lower()
    if status in {"exact", "ambiguous", "unknown"}:
        return status
    return "unknown"


def _member_profile_invite_creator(profile: MemberProfileView | None) -> str:
    if profile is None:
        return ""
    inviter_name = _sanitize_public_text(profile.inviter_name)
    inviter_user_id = _sanitize_public_text(profile.inviter_user_id)
    if inviter_name and inviter_user_id:
        return f"{inviter_name} ({inviter_user_id})"
    return inviter_name or inviter_user_id


def _load_member_profile(repo: Any, ctx: Any, guild_id: str, user_id: str) -> MemberProfileView | None:
    if repo is None:
        return None
    loader = getattr(repo, "get_member_profile", None)
    if callable(loader):
        row = loader(ctx, guild_id, user_id)
        if row is None:
            return None
        return _member_profile_from_row(row, user_id)
    loader = getattr(repo, "get_user_voice_summary", None)
    if callable(loader):
        row = loader(ctx, guild_id, user_id)
        if row is None:
            return None
        return _member_profile_from_row(row, user_id)
    totals_loader = getattr(repo, "list_voice_totals_by_guild", None)
    if callable(totals_loader):
        for row in totals_loader(ctx, guild_id):
            profile = _member_profile_from_row(row, user_id)
            if profile is not None:
                return profile
    return None


def _member_profile_from_row(row: Any, default_user_id: str) -> MemberProfileView | None:
    user_id = _member_profile_string(row, "user_id", "userId") or default_user_id
    if user_id != default_user_id:
        return None
    return MemberProfileView(
        user_id=user_id,
        user_name=_member_profile_string(row, "user_name", "userName"),
        total_for=_timedelta_from_value(
            _member_profile_value(row, "total_for", "totalFor", "total_time", "totalTime") or 0
        ),
        roles=[
            str(role).strip()
            for role in (_member_profile_value(row, "roles", "role_names", "roleNames") or [])
            if str(role).strip()
        ],
        nickname=_member_profile_string(row, "nickname", "current_nickname", "currentNickname"),
        nickname_history=[
            str(value).strip()
            for value in (_member_profile_value(row, "nickname_history", "nicknameHistory") or [])
            if str(value).strip()
        ],
        invite_code=_member_profile_string(row, "invite_code", "inviteCode"),
        invite_url=_member_profile_string(row, "invite_url", "inviteUrl"),
        inviter_user_id=_member_profile_string(row, "inviter_user_id", "inviterUserId"),
        inviter_name=_member_profile_string(row, "inviter_name", "inviterName"),
        attribution_status=_member_profile_string(row, "attribution_status", "attributionStatus").lower(),
    )


def _member_profile_value(row: Any, *names: str) -> Any:
    if isinstance(row, dict):
        for name in names:
            value = row.get(name)
            if value is not None:
                return value
        return None
    for name in names:
        value = getattr(row, name, None)
        if value is not None:
            return value
    return None


def _member_profile_string(row: Any, *names: str) -> str:
    return str(_member_profile_value(row, *names) or "").strip()


VoiceApplicationCommand = voice_application_command
VoiceApplicationCommands = voice_application_commands
