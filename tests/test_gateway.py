from __future__ import annotations

import asyncio
from types import SimpleNamespace

from voice_tracker.domain import SummaryReadyEvent
from voice_tracker.gateway import Service, install_event_listener, summary_from_payload, voice_event_from_discord


class FakeGuild:
    def __init__(self, member) -> None:
        self._member = member

    def get_member(self, user_id):
        return self._member if str(user_id) == str(self._member.id) else None


class FakeSession:
    def __init__(self, member) -> None:
        self.member = member
        self.listeners: list[tuple[object, str]] = []

    def get_guild(self, guild_id):
        return FakeGuild(self.member)

    def add_listener(self, callback, name):
        self.listeners.append((callback, name))


class FakeBus:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def publish_json(self, _ctx, subject: str, value):
        self.calls.append((subject, value))


def test_voice_event_from_discord_uses_member_snapshot() -> None:
    member = SimpleNamespace(id=42, name="alice", user=SimpleNamespace(bot=True))
    session = FakeSession(member)
    update = SimpleNamespace(
        guild_id="123",
        user_id="42",
        before_update=SimpleNamespace(channel_id="old"),
        after=SimpleNamespace(channel_id="new"),
    )

    event = voice_event_from_discord(session, update)

    assert event.guild_id == "123"
    assert event.user_id == "42"
    assert event.user_name == "alice"
    assert event.previous_channel_id == "old"
    assert event.channel_id == "new"
    assert event.is_bot is True


def test_service_install_registers_voice_listener() -> None:
    member = SimpleNamespace(id=42, name="alice", guild=SimpleNamespace(id="123"), user=SimpleNamespace(bot=False))
    session = FakeSession(member)
    bus = FakeBus()
    service = Service(session, bus)

    service.install()
    callback, name = session.listeners[0]
    assert name == "on_voice_state_update"
    asyncio.run(callback(member, SimpleNamespace(channel_id="old"), SimpleNamespace(channel_id="new")))
    assert bus.calls and bus.calls[0][0] == "voice.events"
    assert bus.calls[0][1].guild_id == "123"


def test_install_event_listener_composes_existing_client_event() -> None:
    calls: list[str] = []
    session = SimpleNamespace()

    async def existing(member, before, after):
        calls.append(f"existing:{member.id}:{before.channel_id}:{after.channel_id}")

    async def listener(member, before, after):
        calls.append(f"listener:{member.id}:{before.channel_id}:{after.channel_id}")

    session.on_voice_state_update = existing

    assert install_event_listener(session, "on_voice_state_update", listener) is True

    member = SimpleNamespace(id=42)
    asyncio.run(session.on_voice_state_update(member, SimpleNamespace(channel_id="old"), SimpleNamespace(channel_id="new")))

    assert calls == ["existing:42:old:new", "listener:42:old:new"]


def test_summary_from_payload_decodes_event() -> None:
    event = summary_from_payload(b'{"sessionId":"s1","guildId":"g1","channelId":"c1","message":"hello"}')
    assert event == SummaryReadyEvent(session_id="s1", guild_id="g1", channel_id="c1", message="hello")
