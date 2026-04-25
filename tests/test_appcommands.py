from __future__ import annotations

from typing import Any

from voice_tracker import appcommands
from voice_tracker.discord_models import PERMISSION_ADMINISTRATOR


def test_register_commands_rejects_empty_catalog() -> None:
    prev = appcommands.command_catalog
    appcommands.command_catalog = lambda: []
    try:
        try:
            appcommands.register_commands(None, object(), "app", "guild")
        except ValueError:
            pass
        else:
            raise AssertionError("expected empty catalog error")
    finally:
        appcommands.command_catalog = prev


def test_register_commands_rejects_malformed_catalog() -> None:
    prev = appcommands.command_catalog
    appcommands.command_catalog = lambda: [{}]
    try:
        try:
            appcommands.register_commands(None, object(), "app", "guild")
        except ValueError:
            pass
        else:
            raise AssertionError("expected malformed catalog error")
    finally:
        appcommands.command_catalog = prev


def test_register_commands_rejects_none_command() -> None:
    prev = appcommands.command_catalog
    appcommands.command_catalog = lambda: [None]
    try:
        try:
            appcommands.register_commands(None, object(), "app", "guild")
        except ValueError:
            pass
        else:
            raise AssertionError("expected nil command error")
    finally:
        appcommands.command_catalog = prev


def test_register_commands_rejects_duplicate_names() -> None:
    prev = appcommands.command_catalog
    appcommands.command_catalog = lambda: [{"name": "dup"}, {"name": "dup"}]
    try:
        try:
            appcommands.register_commands(None, object(), "app", "guild")
        except ValueError:
            pass
        else:
            raise AssertionError("expected duplicate name error")
    finally:
        appcommands.command_catalog = prev


def test_default_commands_match_mvp_catalog() -> None:
    commands = {command["name"]: command for command in appcommands.default_commands()}

    assert list(commands) == [
        "settings",
        "jump",
        "inspect",
        "autorole",
        "unmute",
        "dashboard",
        "userinfo",
    ]
    assert "audit" not in commands
    assert "bot-setting" not in commands
    assert "track" not in commands
    assert "track-list" not in commands
    assert "shuffle" not in commands


def test_mvp_command_payloads_have_expected_shapes_and_permissions() -> None:
    commands = {command["name"]: command for command in appcommands.default_commands()}

    admin_commands = {"settings", "inspect", "autorole", "unmute"}
    all_user_commands = {"jump", "dashboard", "userinfo"}
    for name in admin_commands:
        assert str(commands[name].get("default_member_permissions")) == str(PERMISSION_ADMINISTRATOR)
    for name in all_user_commands:
        assert "default_member_permissions" not in commands[name]

    assert _option_names(commands["settings"]["options"]) == ["show", "mode", "summary-set", "summary-clear"]
    mode_option = _nested_option_by_name(commands["settings"]["options"], "mode", "mode")
    assert _choice_names(mode_option) == ["all"]
    assert _channel_types(_option_by_name(commands["jump"]["options"], "channel")) == [2, 13]
    assert _channel_types(_option_by_name(commands["inspect"]["options"], "channel")) == [2, 13]
    assert _option_type(_option_by_name(commands["autorole"]["options"], "role")) == 8
    assert _option_names(commands["unmute"]["options"]) == ["add", "remove", "list"]
    assert _option_type(_nested_option_by_name(commands["unmute"]["options"], "add", "user")) == 6
    assert _option_type(_nested_option_by_name(commands["unmute"]["options"], "remove", "user")) == 6
    assert _option_type(_option_by_name(commands["userinfo"]["options"], "user")) == 6


def _option_names(options: list[Any]) -> list[str]:
    return [str(option["name"]) for option in options]


def _option_by_name(options: list[Any], name: str) -> dict[str, Any]:
    for option in options:
        if option["name"] == name:
            return option
    raise AssertionError(f"missing option {name!r}")


def _nested_option_by_name(options: list[Any], outer_name: str, inner_name: str) -> dict[str, Any]:
    return _option_by_name(_option_by_name(options, outer_name)["options"], inner_name)


def _choice_names(option: dict[str, Any]) -> list[str]:
    choices = option.get("choices", [])
    return [str(choice["name"]) for choice in choices]


def _channel_types(option: dict[str, Any]) -> list[int]:
    return [int(value) for value in option.get("channel_types", [])]


def _option_type(option: dict[str, Any]) -> int:
    return int(option["type"])
