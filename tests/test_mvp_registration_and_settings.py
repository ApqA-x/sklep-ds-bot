from __future__ import annotations

from pathlib import Path

import pytest

from voice_tracker import domain
from voice_tracker.repository import Repository


ROOT = Path(__file__).resolve().parents[1]


class _FakeCollection:
    def __init__(self) -> None:
        self.find_one_result: dict | None = None
        self.update_calls: list[dict[str, object]] = []

    def find_one(self, query: dict[str, object]) -> dict | None:
        return self.find_one_result

    def update_one(self, query: dict[str, object], update: dict[str, object], upsert: bool = False):
        self.update_calls.append({"query": query, "update": update, "upsert": upsert})
        if query.get("_id"):
            current = dict(self.find_one_result or {"_id": query["_id"]})
            current.update(update.get("$set", {}))
            for key, value in update.get("$setOnInsert", {}).items():
                current.setdefault(key, value)
            self.find_one_result = current

        class _Result:
            matched_count = 1

        return _Result()


class _FakeDb:
    def __init__(self) -> None:
        self.collections = {
            "guild_settings": _FakeCollection(),
            "processed_messages": _FakeCollection(),
            "voice_sessions": _FakeCollection(),
            "voice_session_participants": _FakeCollection(),
            "guild_invite_snapshots": _FakeCollection(),
            "invite_catalog": _FakeCollection(),
            "member_join_attributions": _FakeCollection(),
            "member_join_state": _FakeCollection(),
            "member_role_state": _FakeCollection(),
        }

    def __getitem__(self, name: str) -> _FakeCollection:
        return self.collections[name]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _resolve_autorole_accessor(obj: object, names: tuple[str, ...]) -> str:
    for name in names:
        value = getattr(obj, name, None)
        if callable(value):
            return name
    pytest.fail(f"Expected one of these autorole APIs to exist: {', '.join(names)}")


def test_public_slash_command_registration_is_owned_by_services_commands_only() -> None:
    commands_src = _read("services/commands.py")
    other_service_sources = [
        (path, path.read_text(encoding="utf-8"))
        for path in (ROOT / "services").glob("*.py")
        if path.name != "commands.py"
    ]

    assert "register_commands_http" in commands_src
    assert "await register_commands_http(" in commands_src
    assert not (ROOT / "services" / "shuffle.py").exists()
    for path, source in other_service_sources:
        assert "register_commands_http" not in source, f"{path.name} must not bulk register public slash commands"


def test_audit_channel_overwrite_replaces_previous_summary_channel() -> None:
    db = _FakeDb()
    repo = Repository(db)

    repo.upsert_guild_settings(None, domain.GuildSettings(guild_id="g1", summary_channel_id="audit-old"))
    repo.upsert_guild_settings(None, domain.GuildSettings(guild_id="g1", summary_channel_id="audit-new"))

    guild_settings = db.collections["guild_settings"]
    assert len(guild_settings.update_calls) == 2
    assert guild_settings.update_calls[-1]["update"]["$set"]["summaryChannelId"] == "audit-new"
    assert guild_settings.update_calls[-1]["update"]["$set"]["fallbackSummaryChannelId"] == ""


def test_autorole_setting_should_round_trip_auto_role_id() -> None:
    settings = domain.GuildSettings.from_mongo({"_id": "g1", "autoRoleId": "role-1"})
    assert settings is not None
    assert hasattr(settings, "auto_role_id"), "GuildSettings should expose auto_role_id for /autorole"
    assert getattr(settings, "auto_role_id") == "role-1"

    db = _FakeDb()
    repo = Repository(db)
    repo.upsert_guild_settings(None, settings)
    assert db.collections["guild_settings"].update_calls[-1]["update"]["$set"]["autoRoleId"] == "role-1"


def test_autorole_replacement_keeps_only_the_new_safe_setting() -> None:
    repo = Repository(_FakeDb())

    getter_name = _resolve_autorole_accessor(repo, ("get_autorole", "GetAutoRole"))
    setter_name = _resolve_autorole_accessor(repo, ("set_autorole", "SetAutoRole"))

    getter = getattr(repo, getter_name)
    setter = getattr(repo, setter_name)

    setter(None, "g1", "role-old")
    setter(None, "g1", "role-new")

    assert getter(None, "g1") == "role-new"
