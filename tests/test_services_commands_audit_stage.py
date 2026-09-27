from __future__ import annotations

import pytest

from services import commands as commands_service
from voice_tracker import site_audit
from voice_tracker.discord_models import (
    ApplicationCommandInteractionDataOption,
    Interaction,
    InteractionCreate,
    Member,
    PERMISSION_ADMINISTRATOR,
    User,
)


class _AutoroleRepoStub:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str]] = []

    def set_autorole(self, _ctx, guild_id: str, role_id: str) -> None:
        self.set_calls.append((guild_id, role_id))


def test_persist_autorole_raises_on_missing_ids() -> None:
    # R26-04 п.6: тихий no-op давал бы ложный stage="effect"; теперь — ValueError (rejected)
    repo = _AutoroleRepoStub()
    with pytest.raises(ValueError, match="autorole not persisted"):
        commands_service._persist_autorole(repo, "", "123")
    with pytest.raises(ValueError, match="autorole not persisted"):
        commands_service._persist_autorole(repo, "g", "")
    with pytest.raises(ValueError, match="autorole not persisted"):
        commands_service._persist_autorole(repo, "  ", " ")
    assert repo.set_calls == []


def test_persist_autorole_writes_with_both_ids() -> None:
    repo = _AutoroleRepoStub()
    commands_service._persist_autorole(repo, "g1", "123")
    assert repo.set_calls == [("g1", "123")]


class _TrustedServiceStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def handle_trusted_command(self, _ctx, _model, command: str, _options) -> str:
        self.calls.append(command)
        return "Added <@1> to trusted users list."


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
async def test_mutating_route_rejection_string_maps_to_rejected_reason() -> None:
    # R26-04 D1: unknown-подкоманда мутирующего маршрута возвращается строкой и
    # обязана маппиться в reason, а не ложиться как invocation/effect
    service = _TrustedServiceStub()
    result = commands_service._dispatch_trusted_command(
        service,  # type: ignore[arg-type]
        _interaction_model(PERMISSION_ADMINISTRATOR),
        "nope",
        [],
    )
    assert result == "Unknown trusted command."
    assert site_audit.reject_reason_for_result(result) == site_audit.REASON_UNKNOWN
    assert service.calls == []

    result = await commands_service._dispatch_command(
        object(),  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _interaction_model(PERMISSION_ADMINISTRATOR),
        "trusted",
        "nope",
        [ApplicationCommandInteractionDataOption(name="user", value="1")],
        [],
    )
    assert result == "Unknown trusted command."
    assert site_audit.reject_reason_for_result(result) == site_audit.REASON_UNKNOWN


def _effect_proved(ok: bool, root: str, command: str, result: object) -> bool:
    # Ровно то выражение, что считается в on_interaction перед аудитом (R26-04 п.6):
    # on_interaction — замыкание внутри main() c живым client/config/mongo, полный
    # харнесс здесь нецелесообразен, поэтому выражение проверяется как чистый помощник.
    return ok and (root, command) in site_audit.MUTATING_ROUTES and site_audit.reject_reason_for_result(result) is None


@pytest.mark.parametrize(
    ("ok", "root", "command", "result", "expected"),
    [
        # мутация + ok + success-строка → эффект доказан
        (True, "trusted", "add", "Added <@1> to trusted users list.", True),
        # мутация + ok + non-str payload (embed/InteractionMessage) → эффект доказан
        (True, "autorole", "", object(), True),
        # имя мутирующего маршрута без success-доказательства (ok=False) → не эффект
        (False, "trusted", "add", "boom", False),
        # строка-отказ на мутирующем маршруте → не эффект (даже если ok не успели сбросить)
        (True, "trusted", "nope", "Unknown trusted command.", False),
        (True, "trusted", "add", "Insufficient permissions.", False),
        (True, "connect", "", "Command failed. Check service logs.", False),
        # немутрующий маршрут → никогда не эффект
        (True, "trusted", "list", "trusted:list", False),
        (True, "dashboard", "", "ok", False),
    ],
)
def test_effect_proved_expression(
    ok: bool,
    root: str,
    command: str,
    result: object,
    expected: bool,
) -> None:
    assert _effect_proved(ok, root, command, result) is expected
