"""T06 S07: CAS-записи guild_settings на реальной Mongo (порт TEST_MONGO_URI, по умолчанию 27099).

Бот-писатели должны: поднимать SettingsConflict при устаревшей revision, повторять намерение
на свежем документе (mutate_guild_settings), корректно создавать отсутствующие документы и
не затырать параллельные изменения. Симуляция web-писателя — ровно тот же контракт, что
реализован в sklep-ds-bot-web/api/mutations.py: фильтр {_id, revision: expected} + $inc.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from voice_tracker import domain  # noqa: E402
from voice_tracker.repository import Repository, SettingsConflict  # noqa: E402

TEST_MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")


def _server_up() -> bool:
    try:
        client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture()
def repo():
    if not _server_up():
        pytest.skip("test mongod is not running on %s" % TEST_MONGO_URI)
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=3000)
    name = f"voice_tracker_t06bot_{uuid.uuid4().hex[:10]}"
    db = client[name]
    yield Repository(db)
    client.drop_database(name)
    client.close()


def _raw(db, guild_id: str) -> dict:
    return db.guild_settings.find_one({"_id": guild_id}) or {}


def test_cas_creation_then_conflict(repo) -> None:
    guild = "170000000000000101"
    settings = domain.GuildSettings(guild_id=guild)
    settings.summary_channel_id = "200000000000000001"
    repo.upsert_guild_settings(None, settings)
    doc = _raw(repo.db, guild)
    assert doc["revision"] == 1
    assert doc["summaryChannelId"] == "200000000000000001"

    # повторная запись тем же устаревшим объектом (revision=0) — конфликт, а не слепой upsert
    stale = domain.GuildSettings(guild_id=guild)
    stale.summary_channel_id = "200000000000000002"
    with pytest.raises(SettingsConflict):
        repo.upsert_guild_settings(None, stale)
    assert _raw(repo.db, guild)["summaryChannelId"] == "200000000000000001"

    # свежий read-modify-write проходит и поднимает revision
    fresh = repo.get_guild_settings(None, guild)
    assert fresh is not None and fresh.revision == 1
    fresh.activity_channel_id = "200000000000000003"
    repo.upsert_guild_settings(None, fresh)
    doc = _raw(repo.db, guild)
    assert doc["revision"] == 2
    assert doc["activityChannelId"] == "200000000000000003"
    assert doc["summaryChannelId"] == "200000000000000001"


def test_legacy_document_without_revision_migrates_once(repo) -> None:
    guild = "170000000000000102"
    repo.db.guild_settings.insert_one({"_id": guild, "summaryChannelId": "200000000000000010"})
    settings = repo.get_guild_settings(None, guild)
    assert settings is not None and settings.revision == 0
    settings.tracking_mode = "specific"
    repo.upsert_guild_settings(None, settings)
    doc = _raw(repo.db, guild)
    assert doc["revision"] == 1
    assert doc["trackingMode"] == "specific"
    # старое неизвестное поле сохранено? — unknownField не входит в $set писателя, не трогается
    repo.db.guild_settings.update_one({"_id": guild}, {"$set": {"unknownField": {"keep": 1}}})
    again = repo.get_guild_settings(None, guild)
    assert again is not None
    again.summary_channel_id = "200000000000000011"
    repo.upsert_guild_settings(None, again)
    doc = _raw(repo.db, guild)
    assert doc["revision"] == 2
    assert doc["unknownField"] == {"keep": 1}


def test_mutate_retries_conflict_and_reapplies_intent(repo) -> None:
    guild = "170000000000000103"
    repo.mutate_guild_settings(
        None, guild, lambda s: setattr(s, "summary_channel_id", "200000000000000020")
    )
    # внешний писатель (симуляция web) двигает revision между read и write бота
    def external_writer() -> None:
        result = repo.db.guild_settings.update_one(
            {"_id": guild, "revision": 1},
            {"$set": {"activityChannelId": "200000000000000021"}, "$inc": {"revision": 1}},
        )
        assert result.matched_count == 1

    calls = {"n": 0}

    def apply(settings: domain.GuildSettings) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            external_writer()  # конфликт для первой попытки
        settings.soundboard_enforcement_enabled = True

    settings = repo.mutate_guild_settings(None, guild, apply, attempts=3)
    assert calls["n"] == 2
    assert settings.revision == 3
    doc = _raw(repo.db, guild)
    assert doc["soundboardEnforcementEnabled"] is True  # намерение бота применено
    assert doc["activityChannelId"] == "200000000000000021"  # намерение web не потеряно


def test_mutate_apply_false_skips_write(repo) -> None:
    guild = "170000000000000104"
    repo.mutate_guild_settings(None, guild, lambda s: False)
    assert _raw(repo.db, guild) == {}


def test_concurrent_list_adds_both_applied(repo) -> None:
    guild = "170000000000000105"
    repo.mutate_guild_settings(None, guild, lambda s: None)  # создать документ
    barrier = threading.Barrier(2)

    def add(user_id: str) -> None:
        barrier.wait()
        repo.add_trusted_user(None, guild, user_id)

    threads = [threading.Thread(target=add, args=(uid,)) for uid in ("3001", "3002")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    doc = _raw(repo.db, guild)
    assert doc["trustedUserIds"] == ["3001", "3002"]
    assert doc["revision"] == 3  # создание + два добавления


def test_web_bot_interleaving_same_field_conflicts_exactly_once(repo) -> None:
    guild = "170000000000000106"
    repo.mutate_guild_settings(
        None, guild, lambda s: setattr(s, "summary_channel_id", "200000000000000030")
    )
    # оба писателя читают ОДНУ revision до барьера — потом пишут параллельно
    bot_settings = repo.get_guild_settings(None, guild)
    assert bot_settings is not None
    web_current = repo.db.guild_settings.find_one({"_id": guild}) or {}
    web_expected = int(web_current.get("revision") or 0)
    assert bot_settings.revision == web_expected == 1
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def bot_writer() -> None:
        barrier.wait()
        bot_settings.summary_channel_id = "200000000000000031"
        try:
            repo.upsert_guild_settings(None, bot_settings)
            outcome = "bot"
        except SettingsConflict:
            outcome = "bot-conflict"
        with lock:
            results.append(outcome)

    def web_writer() -> None:
        barrier.wait()
        # копия контракта api/mutations.py patch_guild_settings (web)
        result = repo.db.guild_settings.update_one(
            {"_id": guild, "revision": web_expected},
            {"$set": {"summaryChannelId": "200000000000000032"}, "$inc": {"revision": 1}},
        )
        with lock:
            results.append("web" if result.matched_count == 1 else "web-conflict")

    threads = [threading.Thread(target=w) for w in (bot_writer, web_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    plain = [r for r in results if not r.endswith("-conflict")]
    conflicted = [r for r in results if r.endswith("-conflict")]
    assert len(plain) == 1 and len(conflicted) == 1
    final = repo.db.guild_settings.find_one({"_id": guild})
    assert final["revision"] == 2
    assert final["summaryChannelId"] == {
        "bot": "200000000000000031",
        "web": "200000000000000032",
    }[plain[0]]
