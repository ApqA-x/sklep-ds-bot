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
    "audit",
    "bot-setting",
    "track",
    "track-list",
    "jump",
    "inspect",
    "autorole",
    "unmute",
    "dashboard",
    "userinfo",
}

ADMIN_ONLY_TOP_LEVEL_COMMANDS = (
    ("audit", ""),
    ("bot-setting", ""),
    ("track", "add"),
    ("track-list", "clear"),
    ("inspect", "channel"),
    ("autorole", ""),
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
    assert "shuffle" not in commands
    assert "settings" not in commands

    for root, _ in ADMIN_ONLY_TOP_LEVEL_COMMANDS:
        assert str(commands[root].get("default_member_permissions")) == str(PERMISSION_ADMINISTRATOR)


@pytest.mark.parametrize("root, command", ADMIN_ONLY_TOP_LEVEL_COMMANDS)
def test_admin_only_runtime_policy_requires_discord_administrator(root: str, command: str) -> None:
    plain = _interaction()
    manage_guild = _interaction(permissions=PERMISSION_MANAGE_GUILD)
    allowlisted_only = _interaction()
    administrator = _interaction(permissions=PERMISSION_ADMINISTRATOR)

    assert not can_use_voice_command(plain, [], root, command)
    assert not can_use_voice_command(manage_guild, [], root, command)
    assert not can_use_voice_command(allowlisted_only, ["u1"], root, command)
    assert can_use_voice_command(administrator, [], root, command)
