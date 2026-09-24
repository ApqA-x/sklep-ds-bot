from __future__ import annotations

from types import SimpleNamespace

from voice_tracker import site_audit
from voice_tracker.discord_models import ApplicationCommandInteractionDataOption


def _option(name: str, value=None, children: list[ApplicationCommandInteractionDataOption] | None = None):
    return ApplicationCommandInteractionDataOption(name=name, value=value, type="", options=children or [])


class _FakeCollection:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=1)


class _FakeDb:
    def __init__(self):
        self.audit = _FakeCollection()

    def __getitem__(self, name):
        assert name == site_audit.COLL_WEB_AUDIT
        return self.audit


def test_build_command_after_route_and_options():
    options = [
        _option("user", "123456789012345678"),
        _option("group", None, [_option("seconds", 600)]),
    ]
    after = site_audit.build_command_after("settings", "summary-set", options, channel_id="777")
    assert after["command"] == "/settings summary-set"
    assert after["options"] == {"user": "123456789012345678", "seconds": 600}
    assert after["channelId"] == "777"
    assert "reason" not in after


def test_build_command_after_root_only_and_empty_options():
    after = site_audit.build_command_after("jump", "", [])
    assert after == {"command": "/jump"}


def test_build_command_after_truncates_long_strings():
    options = [_option("text", "x" * 500)]
    after = site_audit.build_command_after("dashboard", "", options)
    value = after["options"]["text"]
    assert len(value) == 401
    assert value.endswith("…")


def test_record_command_audit_document_matches_web_schema():
    db = _FakeDb()
    site_audit.record_command_audit(
        db,
        guild_id="g1",
        actor_user_id="u1",
        actor_name="apqa",
        root="trusted",
        command="add",
        after={"command": "/trusted add"},
        ok=True,
    )
    doc = db.audit.docs[0]
    assert doc["guildId"] == "g1"
    assert doc["actorUserId"] == "u1"
    assert doc["actorName"] == "apqa"
    assert doc["action"] == "command.trusted"
    assert doc["before"] is None
    assert doc["after"] == {"command": "/trusted add"}
    assert doc["ok"] is True
    assert doc["source"] == "web"
    assert doc["origin"] == "discord"
    assert doc["at"].tzinfo is not None


def test_safe_record_skips_without_guild_or_db():
    db = _FakeDb()
    site_audit.safe_record_command_audit(db, guild_id="", actor_user_id="u", actor_name="n", root="jump", command="", options=[])
    site_audit.safe_record_command_audit(None, guild_id="g", actor_user_id="u", actor_name="n", root="jump", command="", options=[])
    assert db.audit.docs == []


def test_safe_record_never_raises():
    class _Boom(_FakeDb):
        def __getitem__(self, name):
            raise RuntimeError("mongo down")

    site_audit.safe_record_command_audit(
        _Boom(),
        guild_id="g1",
        actor_user_id="u1",
        actor_name="apqa",
        root="status",
        command="",
        options=[],
        ok=True,
    )


def test_safe_record_writes_reason_on_failure():
    db = _FakeDb()
    site_audit.safe_record_command_audit(
        db,
        guild_id="g1",
        actor_user_id="u1",
        actor_name="apqa",
        root="trusted",
        command="add",
        options=[_option("user", "42")],
        channel_id="9",
        reason=site_audit.REASON_PERMISSIONS,
        ok=False,
    )
    doc = db.audit.docs[0]
    assert doc["ok"] is False
    assert doc["after"]["reason"] == "permissions"
    assert doc["after"]["options"] == {"user": "42"}


def test_stage_distinguishes_invocation_rejection_effect():
    # O10/T08.11: вызов, отказ и подтверждённый эффект — разные факты журнала
    assert site_audit.classify_stage(True, "trusted", "add") == site_audit.STAGE_EFFECT
    assert site_audit.classify_stage(True, "trusted", "list") == site_audit.STAGE_INVOCATION
    assert site_audit.classify_stage(False, "trusted", "add") == site_audit.STAGE_REJECTED
    assert site_audit.classify_stage(False, "jump", "") == site_audit.STAGE_REJECTED

    db = _FakeDb()
    site_audit.safe_record_command_audit(
        db, guild_id="g", actor_user_id="u", actor_name="n",
        root="settings", command="summary-set", options=[],
    )
    assert db.audit.docs[0]["stage"] == "effect"
    site_audit.safe_record_command_audit(
        db, guild_id="g", actor_user_id="u", actor_name="n",
        root="jump", command="", options=[], ok=False, reason=site_audit.REASON_PERMISSIONS,
    )
    assert db.audit.docs[1]["stage"] == "rejected"
    # явная запись без stage — обратно совместимый invocation
    site_audit.record_command_audit(
        db, guild_id="g", actor_user_id="u", actor_name="n",
        root="dashboard", command="", after={"command": "/dashboard"}, ok=True,
    )
    assert db.audit.docs[2]["stage"] == "invocation"
