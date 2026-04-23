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
        self.listeners: list[tuple[object, str]] = []
        FakeClient.instances.append(self)

    def add_listener(self, callback, name: str) -> None:
        self.listeners.append((callback, name))

    def event(self, callback):
        return callback

    async def login(self, _token: str) -> None:
        return None

    async def connect(self) -> None:
        return None


async def _noop(*_args, **_kwargs) -> None:
    return None


async def test_auto_unmute_listener_runs_when_member_is_already_muted(monkeypatch) -> None:
    fake_repo = FakeRepo({"123": ["42"]})
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

    FakeClient.instances.clear()
    await gateway.main()

    client = FakeClient.instances[0]
    callback, name = client.listeners[-1]
    assert name == "on_voice_state_update"

    edit_calls: list[dict[str, object]] = []

    async def edit(**kwargs):
        edit_calls.append(kwargs)

    bot_member = SimpleNamespace(id="999", guild_permissions=SimpleNamespace(mute_members=True))
    guild = SimpleNamespace(
        id="123",
        me=None,
        get_member=lambda user_id: bot_member if str(user_id) == "999" else None,
    )
    member = SimpleNamespace(id="42", bot=False, guild=guild, edit=edit)

    await callback(member, SimpleNamespace(mute=True), SimpleNamespace(mute=True))

    assert edit_calls == [{"mute": False, "reason": "Voice Tracker auto-unmute"}]
