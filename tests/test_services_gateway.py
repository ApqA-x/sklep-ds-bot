from __future__ import annotations

from types import SimpleNamespace

import services.gateway as gateway


class FakeRepo:
    def __init__(self, auto_unmute_ids: dict[str, list[str]]) -> None:
        self.auto_unmute_ids = auto_unmute_ids

    def get_auto_unmute_user_ids(self, _ctx, guild_id: str) -> list[str]:
        return list(self.auto_unmute_ids.get(str(guild_id), []))


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
