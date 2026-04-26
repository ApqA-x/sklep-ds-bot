from __future__ import annotations

from types import SimpleNamespace

import services.gateway as gateway
from voice_tracker import domain


class FakeRepo:
    def __init__(self, auto_unmute_ids: dict[str, list[str]]) -> None:
        self.auto_unmute_ids = auto_unmute_ids

    def get_auto_unmute_user_ids(self, _ctx, guild_id: str) -> list[str]:
        return list(self.auto_unmute_ids.get(str(guild_id), []))

    def get_guild_settings(self, _ctx, guild_id: str):
        return domain.GuildSettings(guild_id=str(guild_id))

    def upsert_guild_settings(self, _ctx, settings: domain.GuildSettings) -> None:
        return None


class FakeMongoClient:
    def __init__(self, _uri: str) -> None:
        self.closed = False

    def __getitem__(self, _name: str):
        return object()

    def close(self) -> None:
        self.closed = True


class FakeNATS:
    async def connect(self, _url: str) -> None:
        return None


class FakeBus:
    def __init__(self, _nats, _secret: str, _name: str) -> None:
        self.closed = False

    async def publish_json(self, _ctx, _subject: str, _value) -> None:
        return None

    async def subscribe(self, _ctx, _subject: str, _repo, _handler) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class FakeClient:
    instances: list[FakeClient] = []

    def __init__(self, *args, **kwargs) -> None:
        self.user = SimpleNamespace(id="999")
        FakeClient.instances.append(self)

    def event(self, callback):
        setattr(self, callback.__name__, callback)
        return callback

    async def login(self, _token: str) -> None:
        return None

    async def connect(self) -> None:
        return None


async def _noop(*_args, **_kwargs) -> None:
    return None


async def _boot_gateway(monkeypatch, fake_repo: FakeRepo) -> object:
    fake_mongo = FakeMongoClient("mongodb://example")

    monkeypatch.setattr(gateway, "configure_logging", lambda _name: None)
    monkeypatch.setattr(gateway, "load_config", lambda: SimpleNamespace(
        discord_token="token",
        event_signing_secret="secret",
        discord_guild_id="123",
        mongo_uri="mongodb://example",
        mongo_db="db",
        nats_url="nats://example",
    ))
    monkeypatch.setattr(gateway, "require_event_signing_secret", lambda _secret: None)
    monkeypatch.setattr(gateway, "MongoClient", lambda _uri: fake_mongo)
    monkeypatch.setattr(gateway, "Repository", lambda _db: fake_repo)
    monkeypatch.setattr(gateway, "NATS", FakeNATS)
    monkeypatch.setattr(gateway, "Bus", FakeBus)
    monkeypatch.setattr(gateway.discord, "Client", FakeClient)
    monkeypatch.setattr(gateway, "_deliver_pending", _noop)
    monkeypatch.setattr(gateway.asyncio, "sleep", _noop)

    FakeClient.instances.clear()
    await gateway.main()

    client = FakeClient.instances[0]
    callback = getattr(client, "on_voice_state_update", None)
    assert callback is not None
    return callback


def _bot_member(*, mute_members: bool = False, deafen_members: bool = False):
    return SimpleNamespace(
        id="999",
        guild_permissions=SimpleNamespace(
            mute_members=mute_members,
            deafen_members=deafen_members,
        ),
    )


async def test_auto_unmute_listener_runs_when_member_is_already_muted(monkeypatch) -> None:
    callback = await _boot_gateway(monkeypatch, FakeRepo({"123": ["42"]}))

    edit_calls: list[dict[str, object]] = []

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    bot_member = _bot_member(mute_members=True)
    guild = SimpleNamespace(
        id="123",
        me=None,
        get_member=lambda user_id: bot_member if str(user_id) == "999" else None,
    )
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit)

    await callback(member, SimpleNamespace(mute=True), SimpleNamespace(mute=True))

    assert edit_calls == [{"mute": False, "reason": "Voice Tracker auto-unmute"}]


async def test_auto_unmute_listener_runs_when_member_is_newly_muted(monkeypatch) -> None:
    callback = await _boot_gateway(monkeypatch, FakeRepo({"123": ["42"]}))

    edit_calls: list[dict[str, object]] = []

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    bot_member = _bot_member(mute_members=True)
    guild = SimpleNamespace(
        id="123",
        me=None,
        get_member=lambda user_id: bot_member if str(user_id) == "999" else None,
    )
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit)

    await callback(member, SimpleNamespace(mute=False), SimpleNamespace(mute=True))

    assert edit_calls == [{"mute": False, "reason": "Voice Tracker auto-unmute"}]


async def test_auto_unmute_listener_normalizes_repo_ids(monkeypatch) -> None:
    callback = await _boot_gateway(monkeypatch, FakeRepo({"123": [42, " 42 ", ""]}))

    edit_calls: list[dict[str, object]] = []

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    bot_member = _bot_member(mute_members=True)
    guild = SimpleNamespace(
        id="123",
        me=None,
        get_member=lambda user_id: bot_member if str(user_id) == "999" else None,
    )
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit)

    await callback(member, SimpleNamespace(mute=False), SimpleNamespace(mute=True))

    assert edit_calls == [{"mute": False, "reason": "Voice Tracker auto-unmute"}]


async def test_auto_unmute_listener_clears_guild_deafen(monkeypatch) -> None:
    callback = await _boot_gateway(monkeypatch, FakeRepo({"123": ["42"]}))

    edit_calls: list[dict[str, object]] = []

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    bot_member = _bot_member(deafen_members=True)
    guild = SimpleNamespace(
        id="123",
        me=None,
        get_member=lambda user_id: bot_member if str(user_id) == "999" else None,
    )
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit)

    await callback(member, SimpleNamespace(deaf=False), SimpleNamespace(deaf=True))

    assert edit_calls == [{"deafen": False, "reason": "Voice Tracker auto-unmute"}]


async def test_auto_unmute_listener_does_not_override_self_deafen(monkeypatch) -> None:
    callback = await _boot_gateway(monkeypatch, FakeRepo({"123": ["42"]}))

    edit_calls: list[dict[str, object]] = []

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    bot_member = _bot_member(mute_members=True, deafen_members=True)
    guild = SimpleNamespace(
        id="123",
        me=None,
        get_member=lambda user_id: bot_member if str(user_id) == "999" else None,
    )
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit)

    await callback(
        member,
        SimpleNamespace(deaf=False, self_deaf=False),
        SimpleNamespace(deaf=False, self_deaf=True),
    )

    assert edit_calls == []


async def test_auto_unmute_listener_does_not_override_self_mute(monkeypatch) -> None:
    callback = await _boot_gateway(monkeypatch, FakeRepo({"123": ["42"]}))

    edit_calls: list[dict[str, object]] = []

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    bot_member = _bot_member(mute_members=True, deafen_members=True)
    guild = SimpleNamespace(
        id="123",
        me=None,
        get_member=lambda user_id: bot_member if str(user_id) == "999" else None,
    )
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit)

    await callback(
        member,
        SimpleNamespace(mute=False, self_mute=False),
        SimpleNamespace(mute=False, self_mute=True),
    )

    assert edit_calls == []


async def test_auto_unmute_listener_clears_mute_without_deafen_permission(monkeypatch) -> None:
    callback = await _boot_gateway(monkeypatch, FakeRepo({"123": ["42"]}))

    edit_calls: list[dict[str, object]] = []

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    bot_member = _bot_member(mute_members=True, deafen_members=False)
    guild = SimpleNamespace(
        id="123",
        me=None,
        get_member=lambda user_id: bot_member if str(user_id) == "999" else None,
    )
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit)

    await callback(
        member,
        SimpleNamespace(mute=False, deaf=False),
        SimpleNamespace(mute=True, deaf=True),
    )

    assert edit_calls == [{"mute": False, "reason": "Voice Tracker auto-unmute"}]


async def test_auto_unmute_listener_retries_until_voice_state_clears(monkeypatch) -> None:
    callback = await _boot_gateway(monkeypatch, FakeRepo({"123": ["42"]}))

    edit_calls: list[dict[str, object]] = []
    refreshed_states = [
        SimpleNamespace(id="42", voice=SimpleNamespace(mute=True)),
        SimpleNamespace(id="42", voice=SimpleNamespace(mute=False)),
    ]

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    def get_member(user_id):
        if str(user_id) == "999":
            return bot_member
        if str(user_id) == "42" and refreshed_states:
            return refreshed_states.pop(0)
        return None

    bot_member = _bot_member(mute_members=True)
    guild = SimpleNamespace(id="123", me=None, get_member=get_member)
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit, voice=SimpleNamespace(mute=True))

    await callback(member, SimpleNamespace(mute=False), SimpleNamespace(mute=True))

    assert edit_calls == [
        {"mute": False, "reason": "Voice Tracker auto-unmute"},
        {"mute": False, "reason": "Voice Tracker auto-unmute"},
    ]


class _ManagedRepo:
    def __init__(self, settings: domain.GuildSettings) -> None:
        self.settings = settings

    def get_guild_settings(self, _ctx, _guild_id: str) -> domain.GuildSettings:
        return self.settings

    def upsert_guild_settings(self, _ctx, settings: domain.GuildSettings) -> None:
        self.settings = settings


class _ManagedVoiceClient:
    def __init__(self, channel: object | None) -> None:
        self.channel = channel
        self.move_calls: list[object] = []

    def is_connected(self) -> bool:
        return self.channel is not None

    async def move_to(self, channel: object) -> None:
        self.move_calls.append(channel)
        self.channel = channel

    async def disconnect(self) -> None:
        self.channel = None


class _ManagedChannel:
    def __init__(self, guild: object, channel_id: str, *, can_move: bool = True) -> None:
        self.guild = guild
        self.id = int(channel_id)
        self.type = gateway.discord.ChannelType.voice
        self.connect_calls = 0
        self._can_move = can_move

    def permissions_for(self, _member: object):
        return SimpleNamespace(view_channel=True, connect=True, move_members=self._can_move)

    async def connect(self) -> _ManagedVoiceClient:
        self.connect_calls += 1
        voice_client = _ManagedVoiceClient(self)
        setattr(self.guild, "voice_client", voice_client)
        bot_member = getattr(self.guild, "members", {}).get("999")
        if bot_member is not None:
            setattr(bot_member, "voice", SimpleNamespace(channel=self))
        return voice_client


class _ManagedGuild:
    def __init__(self, guild_id: str, channel_id: str, *, can_move: bool = True) -> None:
        self.id = int(guild_id)
        self.voice_client: _ManagedVoiceClient | None = None
        self.members: dict[str, object] = {}
        self.channel = _ManagedChannel(self, channel_id, can_move=can_move)

    def get_member(self, user_id: int):
        return self.members.get(str(user_id))

    def get_channel(self, channel_id: int):
        if channel_id == self.channel.id:
            return self.channel
        return None

    async def fetch_channel(self, channel_id: int):
        return self.get_channel(channel_id)


class _ManagedClient:
    def __init__(self, guild: _ManagedGuild, bot_user_id: str = "999") -> None:
        self.guild = guild
        self.guilds = [guild]
        self.voice_clients: list[_ManagedVoiceClient] = []
        self.user = SimpleNamespace(id=bot_user_id)

    def get_guild(self, guild_id: int):
        if guild_id == self.guild.id:
            return self.guild
        return None


async def test_managed_voice_controller_connects_to_configured_channel() -> None:
    settings = domain.GuildSettings(guild_id="123", managed_voice_channel_id="42")
    repo = _ManagedRepo(settings)
    guild = _ManagedGuild("123", "42")
    guild.members["999"] = SimpleNamespace(id="999", guild_permissions=SimpleNamespace(), voice=None)
    client = _ManagedClient(guild)
    controller = gateway.ManagedVoiceController(client=client, repo=repo, guild_id="123")

    await controller.reconcile()

    assert guild.channel.connect_calls == 1
    assert repo.settings.managed_voice_connected_at is not None


async def test_managed_voice_controller_moves_bot_back_to_managed_channel() -> None:
    settings = domain.GuildSettings(guild_id="123", managed_voice_channel_id="42")
    repo = _ManagedRepo(settings)
    guild = _ManagedGuild("123", "42")
    other_channel = SimpleNamespace(id=777)
    guild.voice_client = _ManagedVoiceClient(other_channel)
    guild.members["999"] = SimpleNamespace(id="999", guild_permissions=SimpleNamespace(), voice=SimpleNamespace(channel=other_channel))
    client = _ManagedClient(guild)
    controller = gateway.ManagedVoiceController(client=client, repo=repo, guild_id="123")

    await controller.reconcile()

    assert guild.voice_client is not None
    assert len(guild.voice_client.move_calls) == 1
    assert getattr(guild.voice_client.channel, "id", None) == 42


async def test_soundboard_enforcement_disconnects_member_only_when_enabled() -> None:
    settings = domain.GuildSettings(
        guild_id="123",
        managed_voice_channel_id="42",
        soundboard_enforcement_enabled=True,
    )
    repo = _ManagedRepo(settings)
    guild = _ManagedGuild("123", "42", can_move=True)
    managed_channel = guild.channel

    bot_member = SimpleNamespace(id="999", guild_permissions=SimpleNamespace(), voice=SimpleNamespace(channel=managed_channel))
    moved: list[object] = []

    async def _move_to(channel, reason: str | None = None):
        moved.append((channel, reason))

    user_member = SimpleNamespace(id="42", bot=False, voice=SimpleNamespace(channel=managed_channel), move_to=_move_to)
    guild.members["999"] = bot_member
    guild.members["42"] = user_member

    client = _ManagedClient(guild)
    controller = gateway.ManagedVoiceController(client=client, repo=repo, guild_id="123")
    enforcement = gateway.SoundboardEnforcement(client=client, repo=repo, guild_id="123", voice_controller=controller)

    effect = SimpleNamespace(guild=guild, channel=managed_channel, user_id="42", sound_id="abc")
    await enforcement.on_voice_channel_effect(effect)

    assert len(moved) == 1
    assert moved[0][0] is None


async def test_soundboard_enforcement_uses_effect_user_when_user_id_is_missing() -> None:
    settings = domain.GuildSettings(
        guild_id="123",
        managed_voice_channel_id="42",
        soundboard_enforcement_enabled=True,
    )
    repo = _ManagedRepo(settings)
    guild = _ManagedGuild("123", "42", can_move=True)
    managed_channel = guild.channel

    bot_member = SimpleNamespace(id="999", guild_permissions=SimpleNamespace(), voice=SimpleNamespace(channel=managed_channel))
    moved: list[object] = []

    async def _move_to(channel, reason: str | None = None):
        moved.append((channel, reason))

    user_member = SimpleNamespace(id="42", bot=False, voice=SimpleNamespace(channel=managed_channel), move_to=_move_to)
    guild.members["999"] = bot_member
    guild.members["42"] = user_member

    client = _ManagedClient(guild)
    controller = gateway.ManagedVoiceController(client=client, repo=repo, guild_id="123")
    enforcement = gateway.SoundboardEnforcement(client=client, repo=repo, guild_id="123", voice_controller=controller)

    effect = SimpleNamespace(guild=guild, channel=managed_channel, user=SimpleNamespace(id="42"), sound=SimpleNamespace(id="abc"))
    await enforcement.on_voice_channel_effect(effect)

    assert len(moved) == 1
    assert moved[0][0] is None


async def test_soundboard_enforcement_ignores_effect_without_sender_identity() -> None:
    settings = domain.GuildSettings(
        guild_id="123",
        managed_voice_channel_id="42",
        soundboard_enforcement_enabled=True,
    )
    repo = _ManagedRepo(settings)
    guild = _ManagedGuild("123", "42", can_move=True)
    managed_channel = guild.channel

    bot_member = SimpleNamespace(id="999", guild_permissions=SimpleNamespace(), voice=SimpleNamespace(channel=managed_channel))
    moved: list[object] = []

    async def _move_to(channel, reason: str | None = None):
        moved.append((channel, reason))

    user_member = SimpleNamespace(id="42", bot=False, voice=SimpleNamespace(channel=managed_channel), move_to=_move_to)
    guild.members["999"] = bot_member
    guild.members["42"] = user_member

    client = _ManagedClient(guild)
    controller = gateway.ManagedVoiceController(client=client, repo=repo, guild_id="123")
    enforcement = gateway.SoundboardEnforcement(client=client, repo=repo, guild_id="123", voice_controller=controller)

    effect = SimpleNamespace(guild=guild, channel=managed_channel, member=SimpleNamespace(id="42"), sound_id="abc")
    await enforcement.on_voice_channel_effect(effect)

    assert moved == []
