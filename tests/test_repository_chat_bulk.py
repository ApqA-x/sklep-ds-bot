"""T16 (п.5/L07): массовое удаление Discord-сообщений = tombstone существующим
записям архива. Никакого удаления данных и никаких upsert'ов на невиданные id."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from voice_tracker.repository import Repository

GUILD = "170000000000000000"
CHANNEL = "140000000000000000"


class _Result:
    def __init__(self, modified: int) -> None:
        self.modified_count = modified


class _ChatCollection:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.update_many_calls: list[tuple[dict, dict, bool]] = []

    def insert_one(self, doc: dict[str, Any]) -> None:
        self.documents.append(dict(doc))

    def update_many(self, query: dict, update: dict, upsert: bool = False) -> _Result:
        self.update_many_calls.append((query, update, upsert))
        modified = 0
        ids = query["messageId"]["$in"]
        for idx, doc in enumerate(self.documents):
            if (doc.get("guildId") == query["guildId"]
                    and doc.get("channelId") == query["channelId"]
                    and doc.get("messageId") in ids):
                merged = dict(doc)
                merged.update(update.get("$set", {}))
                self.documents[idx] = merged
                modified += 1
        return _Result(modified)


class _Db:
    def __init__(self) -> None:
        self.chat = _ChatCollection()

    def __getitem__(self, name: str) -> Any:
        if name == "chat_messages":
            return self.chat
        return _ChatCollection()  # прочие коллекции репозитория в этом тесте не используются


def _repo_with(docs: list[dict[str, Any]]) -> tuple[Repository, _Db]:
    db = _Db()
    for doc in docs:
        db.chat.insert_one(doc)
    return Repository(db), db


def _msg(message_id: str, **extra: Any) -> dict[str, Any]:
    doc = {"guildId": GUILD, "channelId": CHANNEL, "messageId": message_id,
           "content": f"text {message_id}", "deletedAt": None}
    doc.update(extra)
    return doc


def test_bulk_delete_tombstones_existing_and_keeps_content() -> None:
    repo, db = _repo_with([_msg("1"), _msg("2"), _msg("3", channelId="999")])
    moment = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

    marked = repo.mark_chat_messages_bulk_deleted(
        None, guild_id=GUILD, channel_id=CHANNEL, message_ids=["1", "2"], deleted_at=moment
    )

    assert marked == 2
    by_id = {d["messageId"]: d for d in db.chat.documents}
    assert by_id["1"]["deletedAt"] == moment and by_id["2"]["deletedAt"] == moment
    # содержимое сохранено (D04), чужой канал не тронут
    assert by_id["1"]["content"] == "text 1"
    assert by_id["3"]["deletedAt"] is None


def test_bulk_delete_never_upserts_unknown_ids() -> None:
    repo, db = _repo_with([_msg("1")])
    marked = repo.mark_chat_messages_bulk_deleted(
        None, guild_id=GUILD, channel_id=CHANNEL, message_ids=["1", "unknown-9"]
    )
    assert marked == 1
    assert len(db.chat.documents) == 1  # ни одной новой записи
    query, update, upsert = db.chat.update_many_calls[0]
    assert upsert is False
    assert set(update) == {"$set"}  # только deletedAt, без $setOnInsert
    assert query["messageId"] == {"$in": ["1", "unknown-9"]}


def test_bulk_delete_input_is_sanitized() -> None:
    repo, db = _repo_with([_msg("1")])
    assert repo.mark_chat_messages_bulk_deleted(
        None, guild_id="", channel_id=CHANNEL, message_ids=["1"]
    ) == 0
    assert repo.mark_chat_messages_bulk_deleted(
        None, guild_id=GUILD, channel_id=CHANNEL, message_ids=["", "  "]
    ) == 0
    assert repo.mark_chat_messages_bulk_deleted(
        None, guild_id=GUILD, channel_id="", message_ids=["1"]
    ) == 0
    assert db.chat.update_many_calls == []  # пустые заходы вообще не идут в БД
