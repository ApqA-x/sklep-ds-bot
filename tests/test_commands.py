from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voice_tracker import commands as commands_module
from voice_tracker import domain
from voice_tracker.commands import (
    ActiveSessionView,
    INSPECT_ACTIVE_ALL_COMMAND,
    INSPECT_ACTIVE_CHANNEL_COMMAND,
    INSPECT_COMMAND_NAME,
    INSPECT_HISTORY_ALL_COMMAND,
    INSPECT_HISTORY_PICK_COMMAND,
    MAX_CLOSED_HISTORY_ITEMS,
    Service,
    SETTINGS_COMMAND_NAME,
    can_use_voice_command,
    command_policy,
    format_duration,
    normalize_snowflake_id,
    option_role_id,
    option_user_id,
    option_int_in_range,
    parse_voice_route,
    registered_voice_command_routes,
    resolve_command_channel,
    voice_application_commands,
)
from voice_tracker.discord_models import (
    ApplicationCommandInteractionData,
    ApplicationCommandInteractionDataOption,
    ApplicationCommandInteractionDataResolved,
    Channel,
    Interaction,
    InteractionCreate,
    Member,
    PERMISSION_ADMINISTRATOR,
    PERMISSION_MANAGE_GUILD,
    User,
)


class FakeRepo:
    def __init__(self) -> None:
        self.settings: dict[str, domain.GuildSettings] = {}
        self.sessions: dict[str, domain.Session] = {}
        self.participants: dict[str, list[domain.ParticipantInterval]] = {}

    def get_guild_settings(self, _ctx, guild_id: str):
        settings = self.settings.get(guild_id)
        return None if settings is None else domain.GuildSettings(
            settings.guild_id,
            settings.tracking_mode,
            list(settings.tracked_channel_ids),
            settings.summary_channel_id,
            settings.created_at,
            settings.updated_at,
            fallback_summary_channel_id=settings.fallback_summary_channel_id,
            auto_role_id=settings.auto_role_id,
            auto_unmute_user_ids=list(settings.auto_unmute_user_ids),
            soundboard_enforcement_enabled=settings.soundboard_enforcement_enabled,
            managed_voice_channel_id=settings.managed_voice_channel_id,
            managed_voice_connected_at=settings.managed_voice_connected_at,
            invite_snapshot_sync_enabled=settings.invite_snapshot_sync_enabled,
            invite_live_attribution_enabled=settings.invite_live_attribution_enabled,
            invite_userinfo_enabled=settings.invite_userinfo_enabled,
            invite_reconciliation_enabled=settings.invite_reconciliation_enabled,
            activity_channel_id=settings.activity_channel_id,
            activity_event_types=list(settings.activity_event_types),
        )

    def upsert_guild_settings(self, _ctx, settings: domain.GuildSettings) -> None:
        self.settings[settings.guild_id] = domain.GuildSettings(
            settings.guild_id,
            settings.tracking_mode,
            list(settings.tracked_channel_ids),
            settings.summary_channel_id,
            settings.created_at,
            settings.updated_at,
            fallback_summary_channel_id=settings.fallback_summary_channel_id,
            auto_role_id=settings.auto_role_id,
            auto_unmute_user_ids=list(settings.auto_unmute_user_ids),
            soundboard_enforcement_enabled=settings.soundboard_enforcement_enabled,
            managed_voice_channel_id=settings.managed_voice_channel_id,
            managed_voice_connected_at=settings.managed_voice_connected_at,
            invite_snapshot_sync_enabled=settings.invite_snapshot_sync_enabled,
            invite_live_attribution_enabled=settings.invite_live_attribution_enabled,
            invite_userinfo_enabled=settings.invite_userinfo_enabled,
            invite_reconciliation_enabled=settings.invite_reconciliation_enabled,
            activity_channel_id=settings.activity_channel_id,
            activity_event_types=list(settings.activity_event_types),
        )

    def list_active_sessions_by_guild(self, _ctx, guild_id: str):
        return [session for session in self.sessions.values() if session.guild_id == guild_id and session.status == domain.SESSION_STATUS_ACTIVE]

    def find_active_session(self, _ctx, guild_id: str, channel_id: str):
        for session in self.sessions.values():
            if session.guild_id == guild_id and session.channel_id == channel_id and session.status == domain.SESSION_STATUS_ACTIVE:
                return session
        return None

    def list_active_participants_by_guild_session(self, _ctx, guild_id: str, session_id: str):
        return [p for p in self.participants.get(session_id, []) if p.active and p.guild_id == guild_id]

    def list_closed_sessions_by_guild_channel(self, _ctx, guild_id: str, channel_id: str, limit: int):
        sessions = [s for s in self.sessions.values() if s.guild_id == guild_id and s.channel_id == channel_id and s.status == domain.SESSION_STATUS_CLOSED]
        sessions.sort(key=lambda session: (_closed_end_time(session), session.started_at), reverse=True)
        return sessions[:limit]

    def list_participants_by_guild_channel_session(self, _ctx, guild_id: str, channel_id: str, session_id: str):
        return [p for p in self.participants.get(session_id, []) if p.guild_id == guild_id and p.channel_id == channel_id]


def _closed_end_time(session: domain.Session) -> datetime:
    return session.ended_at or datetime.min.replace(tzinfo=UTC)


def interaction_with_channels(
    guild_id: str,
    permissions: int,
    channels: dict[str, Channel] | None = None,
    channel_id: str = "",
) -> InteractionCreate:
    return InteractionCreate(
        interaction=Interaction(
            type="application_command",
            guild_id=guild_id,
            channel_id=channel_id,
            member=Member(user=User(id="u1"), permissions=permissions),
            data=ApplicationCommandInteractionData(
                name="settings",
                options=[],
                resolved=ApplicationCommandInteractionDataResolved(channels=channels or {}),
            ),
        )
    )


def option(name: str, value):
    return ApplicationCommandInteractionDataOption(name=name, value=value)


def test_parse_voice_route() -> None:
    root, command, opts = parse_voice_route(
        ApplicationCommandInteractionData(
            name="inspect",
            options=[
                ApplicationCommandInteractionDataOption(
                    name="history",
                    type="subcommand_group",
                    options=[
                        ApplicationCommandInteractionDataOption(
                            name="all",
                            type="subcommand",
                            options=[option("channel", "c1")],
                        )
                    ],
                )
            ],
        )
    )
    assert (root, command, len(opts)) == ("inspect", INSPECT_HISTORY_ALL_COMMAND, 1)

    root, command, opts = parse_voice_route(
        ApplicationCommandInteractionData(
            name="settings",
            options=[
                ApplicationCommandInteractionDataOption(
                    name="mode",
                    type="subcommand",
                    options=[option("mode", "all")],
                )
            ],
        )
    )
    assert (root, command, len(opts)) == ("settings", "mode", 1)

    root, command, opts = parse_voice_route(
        ApplicationCommandInteractionData(
            name="settings",
            options=[
                ApplicationCommandInteractionDataOption(
                    name="summary-set",
                    type="subcommand",
                    options=[option("channel", "t1")],
                )
            ],
        )
    )
    assert (root, command, len(opts), opts[0].name, opts[0].value) == ("settings", "summary-set", 1, "channel", "t1")


def test_parse_voice_route_accepts_numeric_discord_types() -> None:
    root, command, opts = parse_voice_route(
        ApplicationCommandInteractionData(
            name="inspect",
            options=[
                ApplicationCommandInteractionDataOption(
                    name="history",
                    type="2",
                    options=[
                        ApplicationCommandInteractionDataOption(
                            name="pick",
                            type="1",
                            options=[option("pick", 2)],
                        )
                    ],
                )
            ],
        )
    )
    assert (root, command, len(opts)) == ("inspect", INSPECT_HISTORY_PICK_COMMAND, 1)


def test_can_use_voice_command_requires_admin_for_admin_only_commands() -> None:
    plain = InteractionCreate(interaction=Interaction(member=Member(user=User(id="u2"), permissions=0)))
    manage = InteractionCreate(interaction=Interaction(member=Member(permissions=PERMISSION_MANAGE_GUILD)))
    admin = InteractionCreate(interaction=Interaction(member=Member(permissions=PERMISSION_ADMINISTRATOR)))
    allowlisted = InteractionCreate(interaction=Interaction(member=Member(user=User(id="u1"))))

    admin_only_routes = [
        (SETTINGS_COMMAND_NAME, "show"),
        (SETTINGS_COMMAND_NAME, "mode"),
        (SETTINGS_COMMAND_NAME, "soundboard"),
        (SETTINGS_COMMAND_NAME, "summary-set"),
        (SETTINGS_COMMAND_NAME, "summary-clear"),
        (SETTINGS_COMMAND_NAME, "invite-snapshot"),
        (SETTINGS_COMMAND_NAME, "invite-live"),
        (SETTINGS_COMMAND_NAME, "invite-userinfo"),
        (SETTINGS_COMMAND_NAME, "invite-reconcile"),
        (SETTINGS_COMMAND_NAME, "activity-channel-set"),
        (SETTINGS_COMMAND_NAME, "activity-channel-clear"),
        (SETTINGS_COMMAND_NAME, "activity-member-join"),
        (SETTINGS_COMMAND_NAME, "activity-member-leave"),
        (SETTINGS_COMMAND_NAME, "activity-invite-create"),
        (SETTINGS_COMMAND_NAME, "activity-invite-delete"),
        (SETTINGS_COMMAND_NAME, "activity-invite-used"),
        ("connect", ""),
        ("disconnect", ""),
        ("status", ""),
        (INSPECT_COMMAND_NAME, ""),
        ("autorole", ""),
    ]
    for root, command in admin_only_routes:
        assert can_use_voice_command(plain, [], root, command) is False
        assert can_use_voice_command(manage, [], root, command) is False
        assert can_use_voice_command(allowlisted, ["u1"], root, command) is False
        assert can_use_voice_command(admin, [], root, command) is True


def test_can_use_voice_command_all_user_routes_do_not_require_admin() -> None:
    plain = InteractionCreate(interaction=Interaction(member=Member(user=User(id="u2"), permissions=0)))

    assert can_use_voice_command(plain, [], "jump", "") is True
    assert can_use_voice_command(plain, [], "dashboard", "") is True
    assert can_use_voice_command(plain, [], "userinfo", "") is True


def test_member_profile_from_row_reads_invite_attribution_fields() -> None:
    profile = commands_module._member_profile_from_row(
        {
            "userId": "u1",
            "userName": "Example User",
            "totalFor": 90_000,
            "roles": ["role-a"],
            "inviteUrl": "https://discord.gg/abc123",
            "inviterUserId": "555",
            "inviterName": "Invite Builder",
            "attributionStatus": "exact",
        },
        "u1",
    )

    assert profile is not None
    assert profile.user_id == "u1"
    assert profile.user_name == "Example User"
    assert profile.total_for == timedelta(seconds=90)
    assert profile.roles == ["role-a"]
    assert profile.invite_url == "https://discord.gg/abc123"
    assert profile.inviter_user_id == "555"
    assert profile.inviter_name == "Invite Builder"
    assert profile.attribution_status == "exact"


def test_handle_userinfo_command_renders_persisted_invite_attribution() -> None:
    class UserInfoRepo(FakeRepo):
        def get_member_profile(self, _ctx, guild_id: str, user_id: str):
            assert guild_id == "g1"
            assert user_id == "u1"
            return {
                "userId": "u1",
                "userName": "Example User",
                "totalFor": 60_000,
                "inviteUrl": "https://discord.gg/abc123",
                "inviterUserId": "555",
                "inviterName": "@everyone <@123> Invite Builder",
                "attributionStatus": "exact",
            }

    service = Service(UserInfoRepo())
    interaction = InteractionCreate(interaction=Interaction(guild_id="g1", member=Member(user=User(id="u1"))))

    result = service.handle_userinfo_command(None, interaction, "", [])

    assert "User: Example User" in result
    assert "Total voice time: 0:01:00" in result
    assert "Invite used: https://discord.gg/abc123" in result
    assert "Invite created by: Invite Builder (555)" in result
    assert "Invite attribution: exact" in result


def test_voice_application_commands_have_expected_routes() -> None:
    commands = {command.name: command for command in voice_application_commands()}
    assert list(commands) == [
        "settings",
        "connect",
        "disconnect",
        "jump",
        "inspect",
        "autorole",
        "unmute",
        "dashboard",
        "userinfo",
        "status",
    ]

    assert [option.name for option in commands[SETTINGS_COMMAND_NAME].options] == [
        "show",
        "mode",
        "soundboard",
        "summary-set",
        "summary-clear",
        "invite-snapshot",
        "invite-live",
        "invite-userinfo",
        "invite-reconcile",
        "activity-channel-set",
        "activity-channel-clear",
        "activity-member-join",
        "activity-member-leave",
        "activity-invite-create",
        "activity-invite-delete",
        "activity-invite-used",
    ]
    mode_option = next(option for option in commands[SETTINGS_COMMAND_NAME].options if option.name == "mode")
    assert len(mode_option.options) == 1
    assert [choice.name for choice in mode_option.options[0].choices] == [domain.GUILD_TRACKING_MODE_ALL]
    soundboard_option = next(option for option in commands[SETTINGS_COMMAND_NAME].options if option.name == "soundboard")
    assert len(soundboard_option.options) == 1
    assert [choice.name for choice in soundboard_option.options[0].choices] == ["on", "off"]
    assert [option.name for option in commands["connect"].options] == ["channel"]
    assert commands["disconnect"].options == []
    assert [option.name for option in commands[INSPECT_COMMAND_NAME].options] == ["channel"]
    assert [option.name for option in commands["unmute"].options] == ["add", "remove", "list"]
    assert [option.name for option in commands["status"].options] == ["state"]

def test_handle_settings_commands() -> None:
    repo = FakeRepo()
    svc = Service(repo)
    interaction = interaction_with_channels("g1", PERMISSION_MANAGE_GUILD, {"t1": Channel(id="t1", guild_id="g1", type="guild_text")})

    content = svc.handle_settings_command(None, interaction, "mode", [option("mode", domain.GUILD_TRACKING_MODE_NONE)])
    assert "Tracking mode is fixed to 'all'" in content
    assert "tracking mode: all" in content

    content = svc.handle_settings_command(None, interaction, "soundboard", [option("state", "on")])
    assert "soundboard enforcement: on" in content

    content = svc.handle_settings_command(None, interaction, "soundboard", [option("state", "off")])
    assert "soundboard enforcement: off" in content

    content = svc.handle_settings_command(None, interaction, "summary-set", [option("channel", "t1")])
    assert "summary channel: <#t1>" in content

    content = svc.handle_settings_command(None, interaction, "summary-clear", [])
    assert "summary channel: not set" in content

    content = svc.handle_settings_command(None, interaction, "invite-userinfo", [option("state", "off")])
    assert "invite userinfo: off" in content

    content = svc.handle_settings_command(None, interaction, "invite-live", [option("state", "on")])
    assert "invite live attribution: on" in content

    content = svc.handle_settings_command(None, interaction, "activity-channel-set", [option("channel", "t1")])
    assert "activity channel: <#t1>" in content

    content = svc.handle_settings_command(None, interaction, "activity-member-join", [option("state", "off")])
    assert "member_join" not in content

    content = svc.handle_settings_command(None, interaction, "activity-member-join", [option("state", "on")])
    assert "member_join" in content


def test_fallback_summary_channel_is_described() -> None:
    repo = FakeRepo()
    svc = Service(repo)
    svc.remember_fallback_summary_channel(None, "g1", "t1")

    content = svc.handle_settings_command(None, interaction_with_channels("g1", PERMISSION_MANAGE_GUILD), "show", [])
    assert "summary channel: <#t1> (fallback)" in content


def test_voice_command_can_remember_interaction_channel_for_summary_fallback() -> None:
    repo = FakeRepo()
    svc = Service(repo)
    interaction = interaction_with_channels("g1", PERMISSION_MANAGE_GUILD, channel_id="text-from-command")

    svc.remember_fallback_summary_channel(None, interaction.guild_id, interaction.channel_id)

    assert repo.settings["g1"].fallback_summary_channel_id == "text-from-command"


def test_handle_inspect_commands() -> None:
    repo = FakeRepo()
    started = datetime(2026, 4, 5, 18, 0, tzinfo=UTC)
    newer_ended = datetime(2026, 4, 5, 19, 30, tzinfo=UTC)
    older_ended = datetime(2026, 4, 5, 18, 45, tzinfo=UTC)
    repo.sessions["s1"] = domain.Session(id="s1", guild_id="g1", channel_id="c1", status=domain.SESSION_STATUS_ACTIVE, started_at=started)
    repo.participants["s1"] = [domain.ParticipantInterval(id="p1", session_id="s1", guild_id="g1", channel_id="c1", user_id="u1", user_name="alice", joined_at=started + timedelta(minutes=5), active=True)]
    repo.sessions["new"] = domain.Session(id="new", guild_id="g1", channel_id="c1", status=domain.SESSION_STATUS_CLOSED, started_at=newer_ended - timedelta(minutes=30), ended_at=newer_ended)
    repo.sessions["old"] = domain.Session(id="old", guild_id="g1", channel_id="c1", status=domain.SESSION_STATUS_CLOSED, started_at=older_ended - timedelta(hours=1), ended_at=older_ended, ended_by_user_id="u2")
    repo.participants["new"] = [domain.ParticipantInterval(id="p2", session_id="new", guild_id="g1", channel_id="c1", user_id="u3", user_name="carol", joined_at=newer_ended - timedelta(minutes=30), left_at=newer_ended, duration_ms=int(30 * 60 * 1000))]
    repo.participants["old"] = [
        domain.ParticipantInterval(id="p3", session_id="old", guild_id="g1", channel_id="c1", user_id="u1", user_name="alice", joined_at=older_ended - timedelta(hours=1), left_at=older_ended, duration_ms=int(60 * 60 * 1000)),
        domain.ParticipantInterval(id="p4", session_id="old", guild_id="g1", channel_id="c1", user_id="u1", user_name="alice", joined_at=older_ended - timedelta(minutes=35), left_at=older_ended - timedelta(minutes=25), duration_ms=int(10 * 60 * 1000)),
        domain.ParticipantInterval(id="p5", session_id="old", guild_id="g1", channel_id="c1", user_id="u2", user_name="bob", joined_at=older_ended - timedelta(hours=1), left_at=older_ended, duration_ms=int(60 * 60 * 1000)),
    ]
    svc = Service(repo)
    interaction = interaction_with_channels("g1", PERMISSION_ADMINISTRATOR, {"c1": Channel(id="c1", guild_id="g1", type="guild_voice")})

    content = svc.handle_inspect_command(None, interaction, INSPECT_ACTIVE_ALL_COMMAND, [])
    assert "<#c1>" in content

    content = svc.handle_inspect_command(None, interaction, INSPECT_ACTIVE_CHANNEL_COMMAND, [option("channel", "c1")])
    assert "alice" in content and "Channel: <#c1>" in content

    content = svc.handle_inspect_command(None, interaction, INSPECT_HISTORY_ALL_COMMAND, [option("channel", "c1"), option("limit", 5)])
    assert "Recent closed sessions for <#c1>" in content
    assert "1 users" in content and "2 users" in content

    content = svc.handle_inspect_command(None, interaction, INSPECT_HISTORY_PICK_COMMAND, [option("channel", "c1"), option("pick", 2)])
    assert "Session ID: old" in content
    assert "Ended by: bob" in content
    assert "2 intervals" in content


def test_describe_limits_and_ordering() -> None:
    repo = FakeRepo()
    base_started = datetime(2026, 4, 5, 18, 0, tzinfo=UTC)
    for index in range(11):
        session_id = f"s{index}"
        repo.sessions[session_id] = domain.Session(
            id=session_id,
            guild_id="g1",
            channel_id=f"c{index}",
            status=domain.SESSION_STATUS_ACTIVE,
            started_at=base_started + timedelta(minutes=index),
        )
    svc = Service(repo)
    content = svc.describe_active_sessions(None, "g1")
    assert "+1 more sessions" in content

    for index in range(6):
        ended = datetime(2026, 4, 5, 20, 0, tzinfo=UTC) - timedelta(minutes=index)
        started = ended - timedelta(minutes=30)
        session_id = f"c{index}"
        repo.sessions[session_id] = domain.Session(
            id=session_id,
            guild_id="g1",
            channel_id="chan",
            status=domain.SESSION_STATUS_CLOSED,
            started_at=started,
            ended_at=ended,
        )
        repo.participants[session_id] = [
            domain.ParticipantInterval(
                id=f"p{index}",
                session_id=session_id,
                guild_id="g1",
                channel_id="chan",
                user_id="u1",
                user_name="alice",
                joined_at=started,
                left_at=ended,
                duration_ms=int(30 * 60 * 1000),
            )
        ]
    content = svc.describe_closed_session_history(None, "g1", "chan", 5)
    assert "More sessions available." in content


def test_history_and_pick_validation() -> None:
    svc = Service(FakeRepo())
    interaction = interaction_with_channels("g1", PERMISSION_ADMINISTRATOR, {"c1": Channel(id="c1", guild_id="g1", type="guild_voice")})
    with pytest.raises(ValueError, match=f"limit must be between 1 and {MAX_CLOSED_HISTORY_ITEMS}"):
        svc.handle_inspect_command(None, interaction, INSPECT_HISTORY_ALL_COMMAND, [option("channel", "c1"), option("limit", 11)])
    with pytest.raises(ValueError, match=f"pick must be between 1 and {MAX_CLOSED_HISTORY_ITEMS}"):
        svc.handle_inspect_command(None, interaction, INSPECT_HISTORY_PICK_COMMAND, [option("channel", "c1"), option("pick", 0)])


def test_resolve_command_channel_validates_resolution() -> None:
    interaction = interaction_with_channels("g1", PERMISSION_MANAGE_GUILD, {"c1": Channel(id="c1", guild_id="g1", type="guild_text")})
    with pytest.raises(ValueError, match="unsupported channel type"):
        resolve_command_channel(interaction, [option("channel", "c1")], "channel", "guild_voice")

    wrong_guild = interaction_with_channels("g1", PERMISSION_MANAGE_GUILD, {"c1": Channel(id="c1", guild_id="g2", type="guild_voice")})
    with pytest.raises(ValueError, match="channel must belong to this guild"):
        resolve_command_channel(wrong_guild, [option("channel", "c1")], "channel", "guild_voice")


def test_policy_coverage_for_registered_routes_is_deterministic() -> None:
    for root, command in registered_voice_command_routes():
        assert command_policy(root, command) is not None

    assert command_policy("settings", "") is not None
    assert command_policy("connect", "") is not None
    assert command_policy("disconnect", "") is not None
    assert command_policy("status", "") is not None
    assert command_policy("inspect", "channel") is not None
    assert command_policy("autorole", "role") is not None
    assert command_policy("userinfo", "user") is not None
    assert command_policy("audit", "") is None
    assert command_policy("bot-setting", "") is None
    assert command_policy("track", "add") is None
    assert command_policy("track-list", "clear") is None


def test_option_snowflake_parsing_accepts_str_and_int_values() -> None:
    interaction = interaction_with_channels(
        "g1",
        PERMISSION_MANAGE_GUILD,
        {"1234567890": Channel(id="1234567890", guild_id="g1", type="guild_voice")},
    )

    resolved_channel = resolve_command_channel(interaction, [option("channel", 1234567890)], "channel", "guild_voice")
    assert resolved_channel == "1234567890"
    assert option_user_id([option("user", 999)], "user") == "999"
    assert option_role_id([option("role", 777)], "role") == "777"
    assert option_user_id([option("user", "111")], "user") == "111"
    assert normalize_snowflake_id(True) == ""


def test_option_int_in_range_rounds_and_defaults() -> None:
    assert option_int_in_range([], "limit", 5, 1, 10) == 5
    assert option_int_in_range([option("limit", 7.0)], "limit", 5, 1, 10) == 7
    with pytest.raises(ValueError, match="whole number"):
        option_int_in_range([option("limit", 7.5)], "limit", 5, 1, 10)


def test_format_duration_matches_hms_style() -> None:
    assert format_duration(timedelta(hours=1)) == "1:00:00"
