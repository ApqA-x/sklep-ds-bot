from __future__ import annotations

from types import SimpleNamespace

import services.gateway as gateway
from voice_tracker import domain


class FakeRepo:
    def __init__(self, auto_unmute_ids: dict[str, list[str]]) -> None:
        self.auto_unmute_ids = auto_unmute_ids

    def ensure_indexes(self, _ctx) -> None:
        return None

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


def test_member_role_ids_skips_default_role() -> None:
    default_role = SimpleNamespace(id="1", is_default=lambda: True)
    helper_role = SimpleNamespace(id="2", is_default=lambda: False)
    member = SimpleNamespace(roles=[default_role, helper_role])

    assert gateway._member_role_ids(member) == ["2"]


def test_save_member_role_snapshot_persists_normalized_ids() -> None:
    calls: list[tuple[str, str, list[str]]] = []

    def save_member_role_snapshot(
        _ctx,
        guild_id: str,
        user_id: str,
        role_ids: list[str],
        _seen_at,
        *,
        pending_restore: bool = True,
    ):
        assert pending_restore is True
        calls.append((guild_id, user_id, list(role_ids)))

    repo = SimpleNamespace(save_member_role_snapshot=save_member_role_snapshot)
    member = SimpleNamespace(
        id="42",
        guild=SimpleNamespace(id="123"),
        roles=[SimpleNamespace(id="2", is_default=lambda: False), SimpleNamespace(id="2", is_default=lambda: False)],
    )

    gateway._save_member_role_snapshot(repo, member)

    assert calls == [("123", "42", ["2"])]


def test_sync_member_nickname_state_records_current_nickname() -> None:
    calls: list[tuple[str, str, str, str]] = []

    def record_member_nickname(
        _ctx,
        guild_id: str,
        user_id: str,
        nickname: str,
        _seen_at,
        *,
        source: str = "",
        previous_nickname: str | None = None,
    ):
        assert previous_nickname is None
        calls.append((guild_id, user_id, nickname, source))

    repo = SimpleNamespace(record_member_nickname=record_member_nickname)
    member = SimpleNamespace(id="42", guild=SimpleNamespace(id="123"), nick="Raid Nick", bot=False)

    gateway._sync_member_nickname_state(repo, member, source="member_join")

    assert calls == [("123", "42", "Raid Nick", "member_join")]


def test_sync_member_nickname_state_preserves_stored_nickname_for_member_remove() -> None:
    calls: list[tuple[str, str, str, str]] = []

    def get_member_nickname_state(_ctx, guild_id: str, user_id: str):
        assert guild_id == "123"
        assert user_id == "42"
        return SimpleNamespace(nickname="Raid Nick")

    def record_member_nickname(
        _ctx,
        guild_id: str,
        user_id: str,
        nickname: str,
        _seen_at,
        *,
        source: str = "",
        previous_nickname: str | None = None,
    ):
        assert previous_nickname is None
        calls.append((guild_id, user_id, nickname, source))

    repo = SimpleNamespace(
        get_member_nickname_state=get_member_nickname_state,
        record_member_nickname=record_member_nickname,
    )
    member = SimpleNamespace(id="42", guild=SimpleNamespace(id="123"), nick="", bot=False)

    gateway._sync_member_nickname_state(repo, member, source="member_remove")

    assert calls == [("123", "42", "Raid Nick", "member_remove")]


def test_sync_member_nickname_state_preserves_stored_nickname_for_member_join() -> None:
    calls: list[tuple[str, str, str, str]] = []

    def get_member_nickname_state(_ctx, guild_id: str, user_id: str):
        assert guild_id == "123"
        assert user_id == "42"
        return SimpleNamespace(nickname="Raid Nick")

    def record_member_nickname(
        _ctx,
        guild_id: str,
        user_id: str,
        nickname: str,
        _seen_at,
        *,
        source: str = "",
        previous_nickname: str | None = None,
    ):
        assert previous_nickname is None
        calls.append((guild_id, user_id, nickname, source))

    repo = SimpleNamespace(
        get_member_nickname_state=get_member_nickname_state,
        record_member_nickname=record_member_nickname,
    )
    member = SimpleNamespace(id="42", guild=SimpleNamespace(id="123"), nick="", bot=False)

    gateway._sync_member_nickname_state(repo, member, source="member_join")

    assert calls == [("123", "42", "Raid Nick", "member_join")]


def test_sync_member_nickname_state_skips_empty_member_remove_without_stored_nickname() -> None:
    calls: list[tuple[str, str, str, str]] = []

    def get_member_nickname_state(_ctx, guild_id: str, user_id: str):
        assert guild_id == "123"
        assert user_id == "42"
        return None

    def record_member_nickname(
        _ctx,
        guild_id: str,
        user_id: str,
        nickname: str,
        _seen_at,
        *,
        source: str = "",
        previous_nickname: str | None = None,
    ):
        calls.append((guild_id, user_id, nickname, source))

    repo = SimpleNamespace(
        get_member_nickname_state=get_member_nickname_state,
        record_member_nickname=record_member_nickname,
    )
    member = SimpleNamespace(id="42", guild=SimpleNamespace(id="123"), nick="", bot=False)

    gateway._sync_member_nickname_state(repo, member, source="member_remove")

    assert calls == []


def test_record_member_nickname_change_persists_only_real_changes() -> None:
    calls: list[tuple[str, str, str, str]] = []

    def record_member_nickname(
        _ctx,
        guild_id: str,
        user_id: str,
        nickname: str,
        _seen_at,
        *,
        source: str = "",
        previous_nickname: str | None = None,
    ):
        assert previous_nickname == "Old Nick"
        calls.append((guild_id, user_id, nickname, source))

    repo = SimpleNamespace(record_member_nickname=record_member_nickname)
    before = SimpleNamespace(id="42", guild=SimpleNamespace(id="123"), nick="Old Nick")
    after = SimpleNamespace(id="42", guild=SimpleNamespace(id="123"), nick="New Nick", bot=False)

    gateway._record_member_nickname_change(repo, before, after, source="member_update")
    gateway._record_member_nickname_change(repo, after, after, source="member_update")

    assert calls == [("123", "42", "New Nick", "member_update")]


async def test_restore_member_roles_prunes_missing_roles_and_assigns_safe_roles(monkeypatch) -> None:
    restored: list[tuple[str, str, list[str]]] = []

    class _Repo:
        def get_member_role_state(self, _ctx, _guild_id: str, _user_id: str):
            return SimpleNamespace(role_ids=["missing", "2", "3"])

        def mark_member_roles_restored(self, _ctx, guild_id: str, user_id: str, role_ids: list[str], _restored_at):
            restored.append((guild_id, user_id, list(role_ids)))

    assigned: list[int] = []

    async def add_roles(role, reason: str):
        assigned.append(role.id)

    role_two = SimpleNamespace(id=2, is_default=lambda: False, managed=False, permissions=SimpleNamespace(administrator=False), position=1)
    role_three = SimpleNamespace(id=3, is_default=lambda: False, managed=False, permissions=SimpleNamespace(administrator=False), position=1)

    async def fetch_roles():
        return [role_two, role_three]

    guild = SimpleNamespace(id="123", roles=[role_three], fetch_roles=fetch_roles)
    member = SimpleNamespace(id="42", guild=guild, roles=[SimpleNamespace(id="3", is_default=lambda: False)], add_roles=add_roles)

    async def _resolve_bot_member(_client, _guild):
        return SimpleNamespace(id="999")

    def _is_safe(_role, _bot_member) -> bool:
        return True

    monkeypatch.setattr(gateway, "_resolve_bot_member", _resolve_bot_member)
    monkeypatch.setattr(gateway, "_autorole_is_safe", _is_safe)

    await gateway._restore_member_roles(object(), _Repo(), member, source="test")

    assert assigned == [2]
    assert restored == [("123", "42", ["2", "3"])]


async def test_restore_member_roles_keeps_unresolved_ids_when_role_fetch_fails(monkeypatch) -> None:
    snapshots: list[tuple[list[str], bool]] = []

    class _Repo:
        def get_member_role_state(self, _ctx, _guild_id: str, _user_id: str):
            return SimpleNamespace(role_ids=["9"], pending_restore=True)

        def save_member_role_snapshot(self, _ctx, _guild_id: str, _user_id: str, role_ids: list[str], _seen_at, *, pending_restore: bool = True):
            snapshots.append((list(role_ids), pending_restore))

    async def add_roles(_role, reason: str):
        raise AssertionError("should not assign when role list cannot be resolved")

    async def fetch_roles():
        raise RuntimeError("temporary gateway failure")

    guild = SimpleNamespace(id="123", roles=[], fetch_roles=fetch_roles)
    member = SimpleNamespace(id="42", guild=guild, roles=[], add_roles=add_roles)

    async def _resolve_bot_member(_client, _guild):
        return SimpleNamespace(id="999")

    monkeypatch.setattr(gateway, "_resolve_bot_member", _resolve_bot_member)

    restored = await gateway._restore_member_roles(object(), _Repo(), member, source="reconciliation")

    assert restored is False
    assert snapshots == [(["9"], True)]


async def test_reconcile_member_roles_marks_absent_members_pending(monkeypatch) -> None:
    marked_pending: list[str] = []

    class _Repo:
        def list_member_role_states_by_guild(self, _ctx, _guild_id: str, _limit: int = 0):
            return [SimpleNamespace(user_id="42", pending_restore=False)]

        def mark_member_roles_pending(self, _ctx, _guild_id: str, user_id: str):
            marked_pending.append(user_id)

    guild = SimpleNamespace(id=123)
    client = SimpleNamespace(get_guild=lambda guild_id: guild if int(guild_id) == 123 else None, guilds=[guild])

    async def _resolve_member(_guild, _user_id: str):
        return None

    monkeypatch.setattr(gateway, "_resolve_member", _resolve_member)

    await gateway._reconcile_member_roles(client, _Repo(), "123")

    assert marked_pending == ["42"]


async def test_reconcile_member_roles_skips_sync_when_restore_is_deferred(monkeypatch) -> None:
    sync_calls: list[str] = []

    class _Repo:
        def list_member_role_states_by_guild(self, _ctx, _guild_id: str, _limit: int = 0):
            return [SimpleNamespace(user_id="42", pending_restore=True)]

    member = SimpleNamespace(id="42", bot=False, guild=SimpleNamespace(id="123"), roles=[])
    guild = SimpleNamespace(id=123)
    client = SimpleNamespace(get_guild=lambda guild_id: guild if int(guild_id) == 123 else None, guilds=[guild])

    async def _resolve_member(_guild, _user_id: str):
        return member

    async def _restore_member_roles(_client, _repo, _member, *, source: str):
        assert source == "reconciliation"
        return False

    def _sync_member_role_state(_repo, _member):
        sync_calls.append("sync")

    monkeypatch.setattr(gateway, "_resolve_member", _resolve_member)
    monkeypatch.setattr(gateway, "_restore_member_roles", _restore_member_roles)
    monkeypatch.setattr(gateway, "_sync_member_role_state", _sync_member_role_state)

    await gateway._reconcile_member_roles(client, _Repo(), "123")

    assert sync_calls == []


async def test_restore_member_roles_skips_when_reconciliation_not_pending(monkeypatch) -> None:
    touched: list[str] = []

    class _Repo:
        def get_member_role_state(self, _ctx, _guild_id: str, _user_id: str):
            return SimpleNamespace(role_ids=["2"], pending_restore=False)

        def mark_member_roles_restored(self, _ctx, _guild_id: str, _user_id: str, _role_ids: list[str], _restored_at):
            touched.append("restored")

    guild = SimpleNamespace(id="123", roles=[])
    member = SimpleNamespace(id="42", guild=guild, roles=[])

    result = await gateway._restore_member_roles(object(), _Repo(), member, source="reconciliation")

    assert result is True
    assert touched == []


async def test_restore_member_roles_skips_unsafe_role(monkeypatch) -> None:
    assigned: list[int] = []
    restored: list[list[str]] = []

    class _Repo:
        def get_member_role_state(self, _ctx, _guild_id: str, _user_id: str):
            return SimpleNamespace(role_ids=["2"], pending_restore=True)

        def mark_member_roles_restored(self, _ctx, _guild_id: str, _user_id: str, role_ids: list[str], _restored_at):
            restored.append(list(role_ids))

    role_two = SimpleNamespace(id=2, is_default=lambda: False, managed=False, permissions=SimpleNamespace(administrator=False), position=1)

    async def fetch_roles():
        return [role_two]

    async def add_roles(role, reason: str):
        assigned.append(role.id)

    member = SimpleNamespace(id="42", guild=SimpleNamespace(id="123", roles=[role_two], fetch_roles=fetch_roles), roles=[], add_roles=add_roles)

    async def _resolve_bot_member(_client, _guild):
        return SimpleNamespace(id="999")

    monkeypatch.setattr(gateway, "_resolve_bot_member", _resolve_bot_member)
    monkeypatch.setattr(gateway, "_autorole_is_safe", lambda _role, _bot: False)

    result = await gateway._restore_member_roles(object(), _Repo(), member, source="reconciliation")

    assert result is True
    assert assigned == []
    assert restored == [[]]


async def test_restore_member_roles_keeps_pending_when_assignment_fails(monkeypatch) -> None:
    snapshots: list[tuple[list[str], bool]] = []

    class _Repo:
        def get_member_role_state(self, _ctx, _guild_id: str, _user_id: str):
            return SimpleNamespace(role_ids=["2"], pending_restore=True)

        def save_member_role_snapshot(self, _ctx, _guild_id: str, _user_id: str, role_ids: list[str], _seen_at, *, pending_restore: bool = True):
            snapshots.append((list(role_ids), pending_restore))

    role_two = SimpleNamespace(id=2, is_default=lambda: False, managed=False, permissions=SimpleNamespace(administrator=False), position=1)

    async def fetch_roles():
        return [role_two]

    async def add_roles(_role, reason: str):
        raise RuntimeError("temporary role assignment failure")

    member = SimpleNamespace(
        id="42",
        guild=SimpleNamespace(id="123", roles=[role_two], fetch_roles=fetch_roles),
        roles=[],
        add_roles=add_roles,
    )

    async def _resolve_bot_member(_client, _guild):
        return SimpleNamespace(id="999")

    monkeypatch.setattr(gateway, "_resolve_bot_member", _resolve_bot_member)
    monkeypatch.setattr(gateway, "_autorole_is_safe", lambda _role, _bot: True)

    result = await gateway._restore_member_roles(object(), _Repo(), member, source="reconciliation")

    assert result is False
    assert snapshots == [(["2"], True)]


async def test_sync_current_guild_member_roles_skips_pending_restore_members(monkeypatch) -> None:
    synced: list[str] = []

    class _Repo:
        def get_member_role_state(self, _ctx, _guild_id: str, user_id: str):
            if user_id == "42":
                return SimpleNamespace(pending_restore=True)
            return SimpleNamespace(pending_restore=False)

    member_pending = SimpleNamespace(id="42", bot=False, guild=SimpleNamespace(id="123"), roles=[])
    member_regular = SimpleNamespace(id="99", bot=False, guild=SimpleNamespace(id="123"), roles=[])
    guild = SimpleNamespace(id=123, members=[member_pending, member_regular])
    client = SimpleNamespace(get_guild=lambda guild_id: guild if int(guild_id) == 123 else None, guilds=[guild])

    def _sync_member_role_state(_repo, member):
        synced.append(str(member.id))

    monkeypatch.setattr(gateway, "_sync_member_role_state", _sync_member_role_state)

    await gateway._sync_current_guild_member_roles(client, _Repo(), "123")

    assert synced == ["99"]


async def test_sync_current_guild_member_nicknames_skips_bots(monkeypatch) -> None:
    synced: list[str] = []

    def _sync_member_nickname_state(_repo, member, *, source: str = "sync"):
        synced.append(f"{member.id}:{source}")

    guild = SimpleNamespace(
        id=123,
        members=[
            SimpleNamespace(id="42", bot=False, guild=SimpleNamespace(id="123"), nick="One"),
            SimpleNamespace(id="99", bot=True, guild=SimpleNamespace(id="123"), nick="Two"),
        ],
    )
    client = SimpleNamespace(get_guild=lambda guild_id: guild if int(guild_id) == 123 else None, guilds=[guild])

    monkeypatch.setattr(gateway, "_sync_member_nickname_state", _sync_member_nickname_state)

    await gateway._sync_current_guild_member_nicknames(client, object(), "123")

    assert synced == ["42:guild_sync"]


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


class _InviteRepo:
    def __init__(self, snapshot=None, settings: domain.GuildSettings | None = None) -> None:
        self.snapshot = snapshot
        self.settings = settings or domain.GuildSettings(guild_id="123")
        self.catalog_entries: list[object] = []
        self.deleted_codes: list[tuple[str, str, object, str]] = []
        self.attributions: list[object] = []
        self.state_by_member: dict[str, object] = {}

    def get_guild_settings(self, _ctx, _guild_id: str) -> domain.GuildSettings:
        return self.settings

    def get_guild_invite_snapshot(self, _ctx, guild_id: str):
        if self.snapshot is None:
            return None
        return self.snapshot if str(getattr(self.snapshot, "guild_id", "")) == str(guild_id) else None

    def upsert_guild_invite_snapshot(self, _ctx, snapshot: object) -> None:
        self.snapshot = snapshot

    def upsert_invite_catalog_entry(self, _ctx, entry: object) -> None:
        self.catalog_entries.append(entry)

    def mark_invite_deleted(self, _ctx, guild_id: str, code: str, deleted_at, source: str) -> None:
        self.deleted_codes.append((guild_id, code, deleted_at, source))

    def mark_invite_catalog_deleted(self, _ctx, guild_id: str, code: str, deleted_at, source: str) -> None:
        self.mark_invite_deleted(_ctx, guild_id, code, deleted_at, source)

    def create_member_join_attribution(self, _ctx, attribution: object) -> bool:
        attribution_id = str(getattr(attribution, "id", "") or "")
        if any(str(getattr(item, "id", "") or "") == attribution_id for item in self.attributions):
            return False
        self.attributions.append(attribution)
        return True

    def append_member_join_attribution(self, _ctx, attribution: object) -> bool:
        return self.create_member_join_attribution(_ctx, attribution)

    def project_member_join_state(self, _ctx, attribution: object) -> None:
        key = f"{getattr(attribution, 'guild_id', '')}:{getattr(attribution, 'user_id', '')}"
        self.state_by_member[key] = attribution


class _InviteGuild:
    def __init__(self, guild_id: str, invites: list[object], *, vanity_invite: object | None = None) -> None:
        self.id = int(guild_id)
        self._invites = invites
        self._vanity_invite = vanity_invite

    async def invites(self) -> list[object]:
        return list(self._invites)

    async def vanity_invite(self):
        return self._vanity_invite

    def set_invites(self, invites: list[object]) -> None:
        self._invites = list(invites)


class _InviteClient:
    def __init__(self, guild: _InviteGuild) -> None:
        self.guild = guild
        self.guilds = [guild]

    def get_guild(self, guild_id: int):
        if guild_id == self.guild.id:
            return self.guild
        return None


def _make_invite(
    guild: _InviteGuild,
    *,
    code: str,
    uses: int,
    inviter_id: str = "7",
    inviter_name: str = "Owner",
    invite_type: str = gateway.INVITE_TYPE_REGULAR,
):
    inviter = SimpleNamespace(id=inviter_id, name=inviter_name, global_name=inviter_name)
    channel = SimpleNamespace(id=55, guild=guild)
    return SimpleNamespace(
        code=code,
        uses=uses,
        url=f"https://discord.gg/{code}",
        inviter=inviter,
        channel=channel,
        guild=guild,
        invite_type=invite_type,
    )


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


def test_voice_effect_sound_id_prefers_nested_sound_object() -> None:
    effect = SimpleNamespace(sound=SimpleNamespace(id="s-123"), sound_id="s-legacy")

    assert gateway._voice_effect_sound_id(effect) == "s-123"


def test_voice_effect_sound_id_falls_back_to_scalar_fields() -> None:
    effect = SimpleNamespace(sound=None, soundboard_sound_id="s-789")

    assert gateway._voice_effect_sound_id(effect) == "s-789"


async def test_invite_attribution_seed_on_ready_persists_snapshot_and_catalog() -> None:
    guild = _InviteGuild("123", invites=[])
    invite = _make_invite(guild, code="abc", uses=1)
    guild.set_invites([invite])
    repo = _InviteRepo()
    client = _InviteClient(guild)
    controller = gateway.InviteAttributionController(client=client, repo=repo, guild_id="123")

    await controller.seed_on_ready()

    assert repo.snapshot is not None
    stored_codes = [getattr(item, "code", "") for item in getattr(repo.snapshot, "invites", [])]
    assert stored_codes == ["abc"]
    assert [getattr(item, "code", "") for item in repo.catalog_entries] == ["abc"]


async def test_invite_attribution_records_exact_match_and_projects_state() -> None:
    guild = _InviteGuild("123", invites=[])
    previous = _make_invite(guild, code="abc", uses=1)
    current = _make_invite(guild, code="abc", uses=2)
    repo = _InviteRepo(snapshot=domain.GuildInviteSnapshot(guild_id="123", captured_at=gateway._utc_now(), invites=[{
        "code": "abc",
        "uses": 1,
        "url": "https://discord.gg/abc",
        "channelId": "55",
        "inviterUserId": "7",
        "inviterName": "Owner",
        "inviteType": gateway.INVITE_TYPE_REGULAR,
    }]))
    client = _InviteClient(guild)
    controller = gateway.InviteAttributionController(client=client, repo=repo, guild_id="123")
    controller._ready["123"] = True
    guild.set_invites([current])
    member = SimpleNamespace(id="42", bot=False, guild=guild, joined_at=gateway._utc_now())

    await controller.on_member_join(member)

    assert len(repo.attributions) == 1
    attribution = repo.attributions[0]
    assert getattr(attribution, "attribution_status", "") == gateway.ATTRIBUTION_STATUS_EXACT
    assert getattr(attribution, "invite_code", "") == "abc"
    assert getattr(attribution, "invite_url", "") == "https://discord.gg/abc"
    projected = repo.state_by_member["123:42"]
    assert getattr(projected, "invite_code", "") == "abc"


async def test_invite_attribution_records_ambiguous_status_when_multiple_codes_increase() -> None:
    guild = _InviteGuild("123", invites=[])
    current_a = _make_invite(guild, code="abc", uses=2)
    current_b = _make_invite(guild, code="xyz", uses=6)
    repo = _InviteRepo(snapshot=domain.GuildInviteSnapshot(guild_id="123", captured_at=gateway._utc_now(), invites=[
        {
            "code": "abc",
            "uses": 1,
            "url": "https://discord.gg/abc",
            "channelId": "55",
            "inviterUserId": "7",
            "inviterName": "Owner",
            "inviteType": gateway.INVITE_TYPE_REGULAR,
        },
        {
            "code": "xyz",
            "uses": 5,
            "url": "https://discord.gg/xyz",
            "channelId": "55",
            "inviterUserId": "7",
            "inviterName": "Owner",
            "inviteType": gateway.INVITE_TYPE_REGULAR,
        },
    ]))
    client = _InviteClient(guild)
    controller = gateway.InviteAttributionController(client=client, repo=repo, guild_id="123")
    controller._ready["123"] = True
    guild.set_invites([current_a, current_b])
    member = SimpleNamespace(id="42", bot=False, guild=guild, joined_at=gateway._utc_now())

    await controller.on_member_join(member)

    attribution = repo.attributions[0]
    assert getattr(attribution, "attribution_status", "") == gateway.ATTRIBUTION_STATUS_AMBIGUOUS
    assert getattr(attribution, "invite_code", "") == ""
    assert getattr(attribution, "candidate_codes", []) == ["abc", "xyz"]


async def test_invite_attribution_records_unknown_when_seed_is_unavailable() -> None:
    guild = _InviteGuild("123", invites=[])
    repo = _InviteRepo(snapshot=None)
    client = _InviteClient(guild)
    controller = gateway.InviteAttributionController(client=client, repo=repo, guild_id="123")
    member = SimpleNamespace(id="42", bot=False, guild=guild, joined_at=gateway._utc_now())

    await controller.on_member_join(member)

    attribution = repo.attributions[0]
    assert getattr(attribution, "attribution_status", "") == gateway.ATTRIBUTION_STATUS_UNKNOWN
    assert getattr(attribution, "internal_reason", "") == gateway.UNKNOWN_REASON_SEED_UNAVAILABLE
    projected = repo.state_by_member["123:42"]
    assert getattr(projected, "invite_code", "") == ""


async def test_invite_attribution_publishes_callback_for_created_rows() -> None:
    guild = _InviteGuild("123", invites=[])
    current = _make_invite(guild, code="abc", uses=2)
    repo = _InviteRepo(snapshot=domain.GuildInviteSnapshot(guild_id="123", captured_at=gateway._utc_now(), invites=[{
        "code": "abc",
        "uses": 1,
        "url": "https://discord.gg/abc",
        "channelId": "55",
        "inviterUserId": "7",
        "inviterName": "Owner",
        "inviteType": gateway.INVITE_TYPE_REGULAR,
    }]))
    client = _InviteClient(guild)
    published: list[str] = []

    async def _on_attribution(attribution: object) -> None:
        published.append(str(getattr(attribution, "attribution_status", "")))

    controller = gateway.InviteAttributionController(client=client, repo=repo, guild_id="123", on_attribution=_on_attribution)
    controller._ready["123"] = True
    guild.set_invites([current])
    member = SimpleNamespace(id="42", bot=False, guild=guild, joined_at=gateway._utc_now())

    await controller.on_member_join(member)

    assert published == [gateway.ATTRIBUTION_STATUS_EXACT]


async def test_invite_reconciliation_repairs_catalog_only() -> None:
    guild = _InviteGuild("123", invites=[])
    repo = _InviteRepo()
    client = _InviteClient(guild)
    settings = domain.GuildSettings(guild_id="123", invite_reconciliation_enabled=True)
    controller = gateway.InviteAttributionController(client=client, repo=repo, guild_id="123", reconciliation_default=True)
    repo.settings = settings
    now = gateway._utc_now()
    create_entry = SimpleNamespace(
        created_at=now,
        target=SimpleNamespace(code="abc", url="https://discord.gg/abc", channel=SimpleNamespace(id=55)),
        user=SimpleNamespace(id="7", name="Owner", global_name="Owner"),
    )
    delete_entry = SimpleNamespace(
        created_at=now,
        target=SimpleNamespace(code="def", url="https://discord.gg/def", channel=SimpleNamespace(id=55)),
        user=SimpleNamespace(id="8", name="Other", global_name="Other"),
    )

    def _audit_logs(*, limit: int, action):
        async def _iterator():
            if action == getattr(gateway.discord.AuditLogAction, "invite_create", object()):
                yield create_entry
            if action == getattr(gateway.discord.AuditLogAction, "invite_delete", object()):
                yield delete_entry
        return _iterator()

    guild.audit_logs = _audit_logs

    await controller.reconcile_metadata()

    assert [getattr(item, "code", "") for item in repo.catalog_entries] == ["abc"]
    assert repo.deleted_codes and repo.deleted_codes[0][1] == "def"
    assert repo.state_by_member == {}
