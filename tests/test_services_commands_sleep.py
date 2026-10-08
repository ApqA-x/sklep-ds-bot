from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.commands import _dispatch_sleep_command
from voice_tracker.discord_models import (
    ApplicationCommandInteractionDataOption, Interaction, InteractionCreate, User,
)
from voice_tracker.sleep_timers import TimerMutation


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set(self, guild_id, target_user_id, hours, **kwargs):
        self.calls.append(("set", guild_id, target_user_id, hours, kwargs))
        return TimerMutation({}, {"dueAt": datetime(2026, 10, 8, 21, 0, tzinfo=UTC)}, False)

    def cancel(self, guild_id, target_user_id, **kwargs):
        self.calls.append(("cancel", guild_id, target_user_id, kwargs))
        return TimerMutation({}, {"hadActiveTimer": True}, False)

    def get(self, guild_id, target_user_id):
        self.calls.append(("get", guild_id, target_user_id))
        return {"status": "pending", "dueAt": datetime(2026, 10, 8, 21, 0, tzinfo=UTC)}


def _model(guild_id: str = "123", user_id: str = "456") -> InteractionCreate:
    return InteractionCreate(interaction=Interaction(guild_id=guild_id, user=User(id=user_id)))


@pytest.mark.asyncio
async def test_sleep_set_uses_caller_only_and_interaction_id_for_idempotency() -> None:
    store = RecordingStore()
    result = await _dispatch_sleep_command(
        store, SimpleNamespace(id=789), _model(), "set",
        [ApplicationCommandInteractionDataOption(name="hours", value=2)],
    )
    assert "<t:" in result
    assert store.calls == [
        ("set", "123", "456", 2, {"actor_user_id": "456", "source": "slash", "request_id": "789"})
    ]


@pytest.mark.asyncio
async def test_sleep_status_and_cancel_are_scoped_to_caller() -> None:
    store = RecordingStore()
    assert "<t:" in await _dispatch_sleep_command(store, SimpleNamespace(id=789), _model(), "status", [])
    assert "cancelled" in await _dispatch_sleep_command(store, SimpleNamespace(id=790), _model(), "cancel", [])
    assert store.calls == [
        ("get", "123", "456"),
        ("cancel", "123", "456", {"actor_user_id": "456", "source": "slash", "request_id": "790"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [0, 25, True, "2", 2.5])
async def test_sleep_set_rejects_invalid_hours_without_writing(hours: object) -> None:
    store = RecordingStore()
    with pytest.raises(ValueError):
        await _dispatch_sleep_command(
            store, SimpleNamespace(id=789), _model(), "set",
            [ApplicationCommandInteractionDataOption(name="hours", value=hours)],
        )
    assert store.calls == []


@pytest.mark.asyncio
async def test_sleep_rejects_forged_target_option_and_dm() -> None:
    store = RecordingStore()
    with pytest.raises(ValueError):
        await _dispatch_sleep_command(
            store, SimpleNamespace(id=789), _model(), "set",
            [
                ApplicationCommandInteractionDataOption(name="hours", value=2),
                ApplicationCommandInteractionDataOption(name="user", value="999"),
            ],
        )
    with pytest.raises(ValueError):
        await _dispatch_sleep_command(store, SimpleNamespace(id=789), _model(guild_id=""), "status", [])
    assert store.calls == []
