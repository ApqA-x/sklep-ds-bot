from __future__ import annotations

from types import SimpleNamespace

import pytest

from services import commands as commands_service
from voice_tracker.discord_models import (
    ApplicationCommandInteractionDataOption,
    Interaction,
    InteractionCreate,
    Member,
    PERMISSION_ADMINISTRATOR,
    User,
)


def _option_value(options: list[ApplicationCommandInteractionDataOption], name: str) -> str:
    for option in options:
        if option.name == name:
            return str(option.value)
    return ""


class _CanonicalRoutingServiceStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def handle_inspect_command(self, _ctx, _model, command: str, options: list[ApplicationCommandInteractionDataOption]) -> str:
        self.calls.append(("inspect", command, _option_value(options, "channel")))
        return f"inspect:{command}"

    def remember_fallback_summary_channel(self, _ctx, _guild_id: str, _channel_id: str) -> None:
        raise AssertionError("legacy fallback should not be used for canonical admin commands")

    def handle_voice_command(self, _ctx, _model, _root: str, _command: str, _options) -> str:
        raise AssertionError("legacy voice command handler should not be used for canonical admin commands")


def _interaction_model(permissions: int) -> InteractionCreate:
    return InteractionCreate(
        interaction=Interaction(
            type="application_command",
            guild_id="g1",
            channel_id="c1",
            member=Member(user=User(id="u1"), permissions=permissions),
            user=User(id="u1"),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root", "command", "options", "expected_result", "expected_calls"),
    [
        (
            "inspect",
            "",
            [ApplicationCommandInteractionDataOption(name="channel", value=123456789012345678)],
            "inspect:active.channel",
            [("inspect", "active.channel", "123456789012345678")],
        ),
    ],
)
async def test_dispatch_command_routes_canonical_admin_commands_without_legacy_fallback(
    root: str,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
    expected_result: str,
    expected_calls: list[tuple[str, ...]],
) -> None:
    service = _CanonicalRoutingServiceStub()
    result = await commands_service._dispatch_command(
        object(),
        service,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _interaction_model(PERMISSION_ADMINISTRATOR),
        root,
        command,
        options,
        [],
    )

    assert result == expected_result
    assert service.calls == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root", "command"),
    [
        ("audit", ""),
        ("bot-setting", ""),
        ("track", "add"),
        ("track", "remove"),
        ("track", "list"),
        ("track-list", "clear"),
    ],
)
async def test_dispatch_command_rejects_removed_legacy_routes(root: str, command: str) -> None:
    service = _CanonicalRoutingServiceStub()
    result = await commands_service._dispatch_command(
        object(),
        service,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _interaction_model(PERMISSION_ADMINISTRATOR),
        root,
        command,
        [],
        [],
    )

    assert result == "Unknown command."
    assert service.calls == []


@pytest.mark.asyncio
async def test_dispatch_command_rejects_top_level_inspect_channel_without_admin() -> None:
    service = _CanonicalRoutingServiceStub()
    result = await commands_service._dispatch_command(
        object(),
        service,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _interaction_model(0),
        "inspect",
        "",
        [ApplicationCommandInteractionDataOption(name="channel", value=123456789012345678)],
        [],
    )

    assert result == "Insufficient permissions."
    assert service.calls == []


def test_normalize_snowflake_options_casts_int_channel_user_role_values() -> None:
    normalized = commands_service._normalize_snowflake_options(
        [
            ApplicationCommandInteractionDataOption(name="channel", value=101),
            ApplicationCommandInteractionDataOption(name="role", value=202),
            ApplicationCommandInteractionDataOption(name="user", value=303),
            ApplicationCommandInteractionDataOption(name="limit", value=5),
            ApplicationCommandInteractionDataOption(
                name="add",
                options=[ApplicationCommandInteractionDataOption(name="user", value=404)],
            ),
        ]
    )

    assert normalized[0].value == "101"
    assert normalized[1].value == "202"
    assert normalized[2].value == "303"
    assert normalized[3].value == 5
    assert normalized[4].options[0].value == "404"
