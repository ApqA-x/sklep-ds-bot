from __future__ import annotations

import pytest

from voice_tracker import appcommands
from voice_tracker.commands import can_use_voice_command
from voice_tracker.discord_models import (
    Interaction,
    InteractionCreate,
    Member,
    PERMISSION_ADMINISTRATOR,
    PERMISSION_MANAGE_GUILD,
    User,
)


TARGET_TOP_LEVEL_COMMAND_NAMES = {
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
    "trusted",
    "stalker",
}

REMOVED_TOP_LEVEL_COMMAND_NAMES = {
    "audit",
    "bot-setting",
    "track",
    "track-list",
}

ADMIN_ONLY_TOP_LEVEL_COMMAND_NAMES = {
    "settings",
    "connect",
    "disconnect",
    "inspect",
    "autorole",
    "unmute",
    "status",
    "trusted",
}

ADMIN_ONLY_COMMAND_ROUTES = (
    ("settings", ""),
    ("settings", "show"),
    ("settings", "soundboard"),
    ("settings", "summary-set"),
    ("settings", "summary-clear"),
    ("settings", "activity-channel-set"),
    ("settings", "activity-channel-clear"),
    ("settings", "activity"),
    ("connect", ""),
    ("connect", "channel"),
    ("disconnect", ""),
    ("inspect", ""),
    ("inspect", "channel"),
    ("autorole", ""),
    ("autorole", "role"),
    ("unmute", "add"),
    ("unmute", "remove"),
    ("unmute", "list"),
    ("status", ""),
    ("trusted", "add"),
    ("trusted", "remove"),
    ("trusted", "list"),
)

REMOVED_COMMAND_ROUTES = (
    ("audit", ""),
    ("audit", "channel"),
    ("bot-setting", ""),
    ("track", "add"),
    ("track", "remove"),
    ("track", "list"),
    ("track", "clear"),
    ("track-list", "clear"),
)


def _command_map() -> dict[str, dict[str, object]]:
    return {str(command["name"]): command for command in appcommands.default_commands()}


def _interaction(user_id: str = "u1", permissions: int = 0) -> InteractionCreate:
    return InteractionCreate(
        interaction=Interaction(
            member=Member(user=User(id=user_id), permissions=permissions),
            user=User(id=user_id),
        )
    )


def test_mvp_command_catalog_matches_target_top_level_names() -> None:
    commands = _command_map()

    assert set(commands) == TARGET_TOP_LEVEL_COMMAND_NAMES
    assert REMOVED_TOP_LEVEL_COMMAND_NAMES.isdisjoint(commands)
    assert "shuffle" not in commands

    for root in ADMIN_ONLY_TOP_LEVEL_COMMAND_NAMES:
        assert str(commands[root].get("default_member_permissions")) == str(PERMISSION_ADMINISTRATOR)
    for root in TARGET_TOP_LEVEL_COMMAND_NAMES - ADMIN_ONLY_TOP_LEVEL_COMMAND_NAMES:
        assert "default_member_permissions" not in commands[root]


@pytest.mark.parametrize("root, command", ADMIN_ONLY_COMMAND_ROUTES)
def test_admin_only_runtime_policy_requires_discord_administrator(root: str, command: str) -> None:
    plain = _interaction()
    manage_guild = _interaction(permissions=PERMISSION_MANAGE_GUILD)
    allowlisted_only = _interaction()
    administrator = _interaction(permissions=PERMISSION_ADMINISTRATOR)

    assert not can_use_voice_command(plain, [], root, command)
    assert not can_use_voice_command(manage_guild, [], root, command)
    assert not can_use_voice_command(allowlisted_only, ["u1"], root, command)
    assert can_use_voice_command(administrator, [], root, command)


@pytest.mark.parametrize("root, command", REMOVED_COMMAND_ROUTES)
def test_removed_runtime_routes_are_not_supported(root: str, command: str) -> None:
    administrator = _interaction(permissions=PERMISSION_ADMINISTRATOR)

    assert not can_use_voice_command(administrator, [], root, command)
