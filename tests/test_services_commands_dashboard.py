from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from services import commands as commands_service
from voice_tracker.commands import VoiceTotalView


class _DashboardServiceStub:
    def __init__(self, rows: list[VoiceTotalView]) -> None:
        self.rows = rows

    def list_dashboard_totals(self, _ctx, _guild_id: str) -> list[VoiceTotalView]:
        return list(self.rows)


class _FakeResponse:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    async def edit_message(self, **kwargs: object) -> None:
        self.kwargs = dict(kwargs)


class _FakeInteraction:
    def __init__(self, user_id: str = "viewer-1", guild: object | None = None) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.guild = guild
        self.response = _FakeResponse()


def _row(index: int, *, hours: int, minutes: int = 0, seconds: int = 0) -> VoiceTotalView:
    return VoiceTotalView(
        user_id=f"user-{index}",
        user_name=f"Member {index}",
        total_for=timedelta(hours=hours, minutes=minutes, seconds=seconds),
    )


class _FakeClient:
    async def fetch_user(self, _user_id: int):
        return None

    def get_user(self, _user_id: int):
        return None


@pytest.mark.asyncio
async def test_dispatch_dashboard_command_returns_paginated_embed_with_arrow_view() -> None:
    service = _DashboardServiceStub(
        [
            _row(1, hours=12, minutes=34, seconds=56),
            _row(2, hours=3, minutes=5, seconds=7),
        ]
    )

    result = await commands_service._dispatch_dashboard_command(
        _FakeClient(),
        service,  # type: ignore[arg-type]
        SimpleNamespace(guild_id="g1"),
        _FakeInteraction(),
    )

    assert isinstance(result, commands_service.InteractionMessage)
    assert result.embed is not None
    assert result.view is not None
    assert result.embed.title == "Ranking Top"
    assert result.embed.description is not None
    assert "Sorted by voice time" in result.embed.description
    assert "**#1.** Member 1 <@user-1>" in result.embed.description
    assert "Hours: `12:34:56`" in result.embed.description
    assert result.embed.footer.text == "Page 1 of 1 • Total members: 2"

    view = result.view
    labels = {str(getattr(child, "label", "")): bool(getattr(child, "disabled", False)) for child in view.children}
    assert labels == {"<<": True, "<": True, ">": True, ">>": True}


@pytest.mark.asyncio
async def test_dashboard_view_next_arrow_edits_message_to_next_page() -> None:
    rows = [_row(index, hours=index) for index in range(1, 12)]
    view = commands_service.DashboardView(client=_FakeClient(), guild=None, owner_user_id="viewer-1", rows=rows)
    interaction = _FakeInteraction()

    next_button = next(child for child in view.children if getattr(child, "label", "") == ">")
    await next_button.callback(interaction)

    assert interaction.response.kwargs is not None
    edited_embed = interaction.response.kwargs["embed"]
    edited_view = interaction.response.kwargs["view"]
    assert isinstance(edited_embed, commands_service.discord.Embed)
    assert isinstance(edited_view, commands_service.DashboardView)
    assert edited_embed.description is not None
    assert "**#11.** Member 11 <@user-11>" in edited_embed.description
    assert "Hours: `11:00:00`" in edited_embed.description
    assert edited_embed.footer.text == "Page 2 of 2 • Total members: 11"

    labels = {str(getattr(child, "label", "")): bool(getattr(child, "disabled", False)) for child in edited_view.children}
    assert labels["<<"] is False
    assert labels["<"] is False
    assert labels[">"] is True
    assert labels[">>"] is True


@pytest.mark.asyncio
async def test_dashboard_view_rejects_other_user_interactions() -> None:
    view = commands_service.DashboardView(client=_FakeClient(), guild=None, owner_user_id="viewer-1", rows=[_row(1, hours=1)])

    allowed = await view.interaction_check(SimpleNamespace(user=SimpleNamespace(id="viewer-1")))
    rejected = await view.interaction_check(SimpleNamespace(user=SimpleNamespace(id="viewer-2")))

    assert allowed is True
    assert rejected is False


@pytest.mark.asyncio
async def test_dashboard_prefers_live_name_when_stored_name_is_raw_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _DashboardServiceStub(
        [
            VoiceTotalView(user_id="484769112443322370", user_name="484769112443322370", total_for=timedelta(hours=1, minutes=18, seconds=29)),
        ]
    )

    async def _resolve_member(_guild, _user_id: str):
        return SimpleNamespace(display_name="Tiltmoon", name="tiltmoon")

    async def _fetch_user(_client, _user_id: str):
        raise AssertionError("fetch_user should not be needed when guild member resolves")

    monkeypatch.setattr(commands_service, "_resolve_member_by_id", _resolve_member)
    monkeypatch.setattr(commands_service, "_fetch_user_by_id", _fetch_user)

    result = await commands_service._dispatch_dashboard_command(
        _FakeClient(),
        service,  # type: ignore[arg-type]
        SimpleNamespace(guild_id="g1"),
        _FakeInteraction(guild=object()),
    )

    assert isinstance(result, commands_service.InteractionMessage)
    assert result.embed is not None
    assert result.embed.description is not None
    assert "**#1.** Tiltmoon <@484769112443322370>" in result.embed.description
    assert "484769112443322370 <@484769112443322370>" not in result.embed.description


@pytest.mark.asyncio
async def test_dashboard_falls_back_to_mention_only_when_no_real_name_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _DashboardServiceStub(
        [
            VoiceTotalView(user_id="352316913528995842", user_name="", total_for=timedelta(seconds=25)),
        ]
    )

    async def _resolve_member(_guild, _user_id: str):
        return None

    async def _fetch_user(_client, _user_id: str):
        return None

    monkeypatch.setattr(commands_service, "_resolve_member_by_id", _resolve_member)
    monkeypatch.setattr(commands_service, "_fetch_user_by_id", _fetch_user)

    result = await commands_service._dispatch_dashboard_command(
        _FakeClient(),
        service,  # type: ignore[arg-type]
        SimpleNamespace(guild_id="g1"),
        _FakeInteraction(),
    )

    assert isinstance(result, commands_service.InteractionMessage)
    assert result.embed is not None
    assert result.embed.description is not None
    assert "**#1.** <@352316913528995842>" in result.embed.description
    assert "352316913528995842 <@352316913528995842>" not in result.embed.description
