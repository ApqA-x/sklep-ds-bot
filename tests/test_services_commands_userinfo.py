from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services import commands as commands_service
from voice_tracker.discord_models import ApplicationCommandInteractionDataOption


class _Asset:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeMember:
    def __init__(self) -> None:
        self.display_name = "ApqA"
        self.name = "apqa_"
        self.global_name = "ApqA"
        self.nick = "ApqA Nick"
        self.status = "online"
        self.joined_at = datetime(2025, 11, 17, 12, 0, tzinfo=UTC)
        self.created_at = datetime(2020, 1, 21, 9, 0, tzinfo=UTC)
        self.display_avatar = _Asset("https://cdn.test/avatar.png")
        self.display_banner = _Asset("https://cdn.test/banner.png")


class _FakeUser:
    def __init__(self) -> None:
        self.display_name = "ApqA"
        self.name = "apqa_"
        self.global_name = "ApqA"
        self.created_at = datetime(2020, 1, 21, 9, 0, tzinfo=UTC)
        self.display_avatar = _Asset("https://cdn.test/avatar.png")
        self.banner = _Asset("https://cdn.test/banner.png")


class _FakeInteractionUser:
    def __init__(self, user_id: str) -> None:
        self.id = user_id


class _FakeInteraction:
    def __init__(self, user_id: str = "669151106814967819") -> None:
        self.guild = object()
        self.user = _FakeInteractionUser(user_id)


@pytest.mark.asyncio
async def test_dispatch_userinfo_command_builds_embed_with_presence_status_and_profile_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _FakeMember()
    user = _FakeUser()
    captured: dict[str, str] = {}

    async def _resolve_member(_guild, _user_id: str):
        captured["user_id"] = _user_id
        return member

    async def _fetch_user(_client, _user_id: str):
        return user

    def _load_profile(_ctx, _guild_id: str, _user_id: str):
        return SimpleNamespace(total_for=timedelta(hours=2, minutes=30))

    monkeypatch.setattr(commands_service, "_resolve_member_by_id", _resolve_member)
    monkeypatch.setattr(commands_service, "_fetch_user_by_id", _fetch_user)
    service = SimpleNamespace(get_member_profile=_load_profile)

    embed = await commands_service._dispatch_userinfo_command(
        object(),
        service,
        SimpleNamespace(guild_id="g1"),
        _FakeInteraction(),
        [ApplicationCommandInteractionDataOption(name="user", value=669151106814967819)],
    )

    assert captured["user_id"] == "669151106814967819"
    assert embed.title == "Information about ApqA"
    assert embed.description is not None
    assert "User ID: `669151106814967819`" in embed.description
    assert "Username: apqa_ (ApqA)" in embed.description
    assert "Status: 🟢 Online" in embed.description
    assert "Total voice time: 2:30:00" in embed.description
    assert "Joined at: <t:" in embed.description
    assert "Registered at: <t:" in embed.description
    assert "Nickname:" not in embed.description
    assert "Avatar:" not in embed.description
    assert "Banner:" not in embed.description
    assert embed.thumbnail.url == "https://cdn.test/avatar.png"
    assert embed.image.url == "https://cdn.test/banner.png"


@pytest.mark.asyncio
async def test_dispatch_userinfo_command_uses_fetched_user_banner_when_cached_member_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _FakeMember()
    member.display_banner = None
    user = _FakeUser()
    user.banner = _Asset("https://cdn.test/fetched-banner.png")

    async def _resolve_member(_guild, _user_id: str):
        return member

    async def _fetch_user(_client, _user_id: str):
        return user

    def _load_profile(_ctx, _guild_id: str, _user_id: str):
        return None

    monkeypatch.setattr(commands_service, "_resolve_member_by_id", _resolve_member)
    monkeypatch.setattr(commands_service, "_fetch_user_by_id", _fetch_user)
    service = SimpleNamespace(get_member_profile=_load_profile)

    embed = await commands_service._dispatch_userinfo_command(
        object(),
        service,
        SimpleNamespace(guild_id="g1"),
        _FakeInteraction(),
        [ApplicationCommandInteractionDataOption(name="user", value="669151106814967819")],
    )

    assert embed.description is not None
    assert "Status: 🟢 Online" in embed.description
    assert embed.image.url == "https://cdn.test/fetched-banner.png"


@pytest.mark.asyncio
async def test_dispatch_userinfo_command_marks_missing_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    member = _FakeMember()
    member.display_banner = None
    user = _FakeUser()
    user.banner = None

    async def _resolve_member(_guild, _user_id: str):
        return member

    async def _fetch_user(_client, _user_id: str):
        return user

    def _load_profile(_ctx, _guild_id: str, _user_id: str):
        return None

    monkeypatch.setattr(commands_service, "_resolve_member_by_id", _resolve_member)
    monkeypatch.setattr(commands_service, "_fetch_user_by_id", _fetch_user)
    service = SimpleNamespace(get_member_profile=_load_profile)

    embed = await commands_service._dispatch_userinfo_command(
        object(),
        service,
        SimpleNamespace(guild_id="g1"),
        _FakeInteraction(),
        [ApplicationCommandInteractionDataOption(name="user", value="669151106814967819")],
    )

    assert embed.description is not None
    assert "Banner:" not in embed.description
    assert "Total voice time: 0:00:00" in embed.description
    assert not getattr(embed.image, "url", None)
