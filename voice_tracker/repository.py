from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .domain import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_CLOSED,
    GuildSettings,
    ParticipantInterval,
    Session,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _collection(db: Any, name: str) -> Any:
    if db is None:
        raise ValueError("database is nil")
    if hasattr(db, "__getitem__"):
        return db[name]
    return getattr(db, name)


def _is_duplicate_key_error(err: Exception) -> bool:
    if getattr(err, "code", None) == 11000:
        return True
    return err.__class__.__name__.lower() == "duplicatekeyerror"


def _cursor_all(cursor: Any) -> list[dict[str, Any]]:
    if cursor is None:
        return []
    if hasattr(cursor, "to_list"):
        return list(cursor.to_list(None))
    return list(cursor)


class Repository:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.guild_settings = _collection(db, "guild_settings")
        self.messages = _collection(db, "processed_messages")
        self.sessions = _collection(db, "voice_sessions")
        self.participants = _collection(db, "voice_session_participants")

    def ensure_indexes(self, _ctx: Any = None) -> None:
        self.sessions.create_index(
            [("status", 1), ("guildId", 1), ("channelId", 1)],
            unique=True,
            partialFilterExpression={"status": SESSION_STATUS_ACTIVE},
        )
        self.sessions.create_index([("status", 1), ("guildId", 1), ("channelId", 1), ("endedAt", -1)])
        self.sessions.create_index([("status", 1), ("closedEventPublishedAt", 1)])

        self.participants.create_index([("sessionId", 1), ("active", 1)])
        self.participants.create_index([("guildId", 1), ("sessionId", 1), ("active", 1)])
        self.participants.create_index([("guildId", 1), ("channelId", 1), ("sessionId", 1)])
        self.participants.create_index(
            [("sessionId", 1), ("userId", 1), ("active", 1)],
            unique=True,
            partialFilterExpression={"active": True},
        )

        self.messages.create_index([("subject", 1), ("messageId", 1)], unique=True)
        self.messages.create_index([("createdAt", 1)], expireAfterSeconds=7200)

    def claim_message(self, _ctx: Any, subject: str, message_id: str, issuer: str, issued_at: int) -> bool:
        try:
            self.messages.insert_one(
                {
                    "subject": subject,
                    "messageId": message_id,
                    "issuer": issuer,
                    "issuedAt": issued_at,
                    "createdAt": _utc_now(),
                }
            )
        except Exception as err:
            if _is_duplicate_key_error(err):
                return False
            raise
        return True

    def get_guild_settings(self, _ctx: Any, guild_id: str) -> GuildSettings | None:
        data = self.guild_settings.find_one({"_id": guild_id})
        return GuildSettings.from_mongo(data)

    def upsert_guild_settings(self, _ctx: Any, settings: GuildSettings | None) -> None:
        if settings is None or settings.guild_id == "":
            return
        now = _utc_now()
        if settings.created_at is None:
            settings.created_at = now
        settings.updated_at = now
        self.guild_settings.update_one(
            {"_id": settings.guild_id},
            {
                "$set": {
                    "trackingMode": settings.tracking_mode,
                    "trackedChannelIds": settings.tracked_channel_ids,
                    "summaryChannelId": settings.summary_channel_id,
                    "fallbackSummaryChannelId": settings.fallback_summary_channel_id,
                    "autoRoleId": settings.auto_role_id,
                    "autoUnmuteUserIds": settings.auto_unmute_user_ids,
                    "updatedAt": settings.updated_at,
                },
                "$setOnInsert": {"createdAt": settings.created_at},
            },
            upsert=True,
        )

    def get_autorole(self, ctx: Any, guild_id: str) -> str:
        settings = self.get_guild_settings(ctx, guild_id)
        if settings is None:
            return ""
        return settings.auto_role_id

    def set_autorole(self, ctx: Any, guild_id: str, role_id: str) -> str:
        settings = self.get_guild_settings(ctx, guild_id)
        if settings is None:
            settings = GuildSettings(guild_id=guild_id)
        settings.auto_role_id = str(role_id or "").strip()
        self.upsert_guild_settings(ctx, settings)
        return settings.auto_role_id

    def get_auto_unmute_user_ids(self, ctx: Any, guild_id: str) -> list[str]:
        settings = self.get_guild_settings(ctx, guild_id)
        if settings is None:
            return []
        return list(settings.auto_unmute_user_ids)

    def add_auto_unmute_user(self, ctx: Any, guild_id: str, user_id: str) -> list[str]:
        settings = self.get_guild_settings(ctx, guild_id)
        if settings is None:
            settings = GuildSettings(guild_id=guild_id)
        user_id = str(user_id or "").strip()
        if user_id and user_id not in settings.auto_unmute_user_ids:
            settings.auto_unmute_user_ids = sorted({*settings.auto_unmute_user_ids, user_id})
        self.upsert_guild_settings(ctx, settings)
        return list(settings.auto_unmute_user_ids)

    def remove_auto_unmute_user(self, ctx: Any, guild_id: str, user_id: str) -> list[str]:
        settings = self.get_guild_settings(ctx, guild_id)
        if settings is None:
            settings = GuildSettings(guild_id=guild_id)
        user_id = str(user_id or "").strip()
        settings.auto_unmute_user_ids = [uid for uid in settings.auto_unmute_user_ids if uid != user_id]
        self.upsert_guild_settings(ctx, settings)
        return list(settings.auto_unmute_user_ids)

    def list_closed_sessions_pending_notification(self, _ctx: Any) -> list[Session]:
        cursor = self.sessions.find(
            {
                "status": SESSION_STATUS_CLOSED,
                "$or": [
                    {"closedEventPublishedAt": {"$exists": False}},
                    {"closedEventPublishedAt": None},
                ],
            }
        )
        return [session for session in (Session.from_mongo(doc) for doc in _cursor_all(cursor)) if session is not None]

    def list_closed_sessions_pending_summary(self, _ctx: Any) -> list[Session]:
        cursor = self.sessions.find(
            {
                "status": SESSION_STATUS_CLOSED,
                "$or": [
                    {"summaryGeneratedAt": {"$exists": False}},
                    {"summaryGeneratedAt": None},
                ],
            }
        )
        return [session for session in (Session.from_mongo(doc) for doc in _cursor_all(cursor)) if session is not None]

    def list_closed_sessions_by_guild_channel(self, _ctx: Any, guild_id: str, channel_id: str, limit: int) -> list[Session]:
        if limit <= 0:
            limit = 1
        cursor = self.sessions.find(
            {"status": SESSION_STATUS_CLOSED, "guildId": guild_id, "channelId": channel_id}
        ).sort([("endedAt", -1), ("startedAt", -1)]).limit(limit)
        return [session for session in (Session.from_mongo(doc) for doc in _cursor_all(cursor)) if session is not None]

    def list_summaries_pending_delivery(self, _ctx: Any) -> list[Session]:
        cursor = self.sessions.find(
            {
                "summaryGeneratedAt": {"$exists": True},
                "$and": [
                    {
                        "$or": [
                            {"summaryDeliveredAt": {"$exists": False}},
                            {"summaryDeliveredAt": None},
                        ]
                    },
                    {
                        "$or": [
                            {"summaryDeliveryClaimedAt": {"$exists": False}},
                            {"summaryDeliveryClaimedAt": None},
                        ]
                    },
                ],
            }
        )
        return [session for session in (Session.from_mongo(doc) for doc in _cursor_all(cursor)) if session is not None]

    def create_session(self, _ctx: Any, session: Session) -> None:
        if session.id == "":
            session.id = str(uuid4())
        now = _utc_now()
        if session.created_at is None:
            session.created_at = now
        if session.updated_at is None:
            session.updated_at = now
        if session.status == "":
            session.status = SESSION_STATUS_ACTIVE
        self.sessions.insert_one(session.to_mongo())

    def find_active_session(self, _ctx: Any, guild_id: str, channel_id: str) -> Session | None:
        data = self.sessions.find_one({"status": SESSION_STATUS_ACTIVE, "guildId": guild_id, "channelId": channel_id})
        return Session.from_mongo(data)

    def list_active_sessions(self, _ctx: Any) -> list[Session]:
        cursor = self.sessions.find({"status": SESSION_STATUS_ACTIVE})
        return [session for session in (Session.from_mongo(doc) for doc in _cursor_all(cursor)) if session is not None]

    def list_active_sessions_by_guild(self, _ctx: Any, guild_id: str) -> list[Session]:
        cursor = self.sessions.find({"status": SESSION_STATUS_ACTIVE, "guildId": guild_id})
        return [session for session in (Session.from_mongo(doc) for doc in _cursor_all(cursor)) if session is not None]

    def get_session_by_id(self, _ctx: Any, session_id: str) -> Session | None:
        return Session.from_mongo(self.sessions.find_one({"_id": session_id}))

    def close_session(self, _ctx: Any, session_id: str, ended_at: datetime, ended_by_user_id: str) -> None:
        self.sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": SESSION_STATUS_CLOSED,
                    "endedAt": ended_at,
                    "endedByUserId": ended_by_user_id,
                    "updatedAt": _utc_now(),
                }
            },
        )

    def mark_session_closed_event_published(self, _ctx: Any, session_id: str, published_at: datetime) -> None:
        self.sessions.update_one(
            {"_id": session_id},
            {"$set": {"closedEventPublishedAt": published_at, "updatedAt": _utc_now()}},
        )

    def mark_session_summary_ready(
        self,
        _ctx: Any,
        session_id: str,
        summary_channel_id: str,
        summary_message: str,
        generated_at: datetime,
    ) -> None:
        self.sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "summaryChannelId": summary_channel_id,
                    "summaryMessage": summary_message,
                    "summaryGeneratedAt": generated_at,
                    "updatedAt": _utc_now(),
                }
            },
        )

    def claim_session_summary_delivery(self, _ctx: Any, session_id: str, claimed_at: datetime) -> bool:
        result = self.sessions.update_one(
            {
                "_id": session_id,
                "summaryGeneratedAt": {"$exists": True},
                "summaryDeliveredAt": {"$exists": False},
                "summaryDeliveryClaimedAt": {"$exists": False},
            },
            {"$set": {"summaryDeliveryClaimedAt": claimed_at, "updatedAt": _utc_now()}},
        )
        return bool(getattr(result, "matched_count", 0) > 0)

    def release_session_summary_delivery_claim(self, _ctx: Any, session_id: str) -> None:
        self.sessions.update_one(
            {"_id": session_id},
            {"$unset": {"summaryDeliveryClaimedAt": ""}, "$set": {"updatedAt": _utc_now()}},
        )

    def mark_session_summary_delivered(self, _ctx: Any, session_id: str, delivered_at: datetime) -> None:
        self.sessions.update_one(
            {"_id": session_id},
            {"$set": {"summaryDeliveredAt": delivered_at, "updatedAt": _utc_now()}},
        )

    def create_participant(self, _ctx: Any, participant: ParticipantInterval) -> None:
        if participant.id == "":
            participant.id = str(uuid4())
        if participant.joined_at is None:
            participant.joined_at = _utc_now()
        participant.active = True
        self.participants.insert_one(participant.to_mongo())

    def find_active_participant(self, _ctx: Any, session_id: str, user_id: str) -> ParticipantInterval | None:
        return ParticipantInterval.from_mongo(
            self.participants.find_one({"sessionId": session_id, "userId": user_id, "active": True})
        )

    def list_active_participants(self, _ctx: Any, session_id: str) -> list[ParticipantInterval]:
        cursor = self.participants.find({"sessionId": session_id, "active": True})
        return [p for p in (ParticipantInterval.from_mongo(doc) for doc in _cursor_all(cursor)) if p is not None]

    def list_active_participants_by_guild_session(self, _ctx: Any, guild_id: str, session_id: str) -> list[ParticipantInterval]:
        cursor = self.participants.find({"guildId": guild_id, "sessionId": session_id, "active": True})
        return [p for p in (ParticipantInterval.from_mongo(doc) for doc in _cursor_all(cursor)) if p is not None]

    def list_participants_by_session(self, _ctx: Any, session_id: str) -> list[ParticipantInterval]:
        cursor = self.participants.find({"sessionId": session_id})
        return [p for p in (ParticipantInterval.from_mongo(doc) for doc in _cursor_all(cursor)) if p is not None]

    def list_participants_by_guild_channel_session(
        self,
        _ctx: Any,
        guild_id: str,
        channel_id: str,
        session_id: str,
    ) -> list[ParticipantInterval]:
        cursor = self.participants.find({"guildId": guild_id, "channelId": channel_id, "sessionId": session_id})
        return [p for p in (ParticipantInterval.from_mongo(doc) for doc in _cursor_all(cursor)) if p is not None]

    def close_participant(self, _ctx: Any, participant_id: str, left_at: datetime, duration_ms: int) -> None:
        self.participants.update_one(
            {"_id": participant_id},
            {"$set": {"active": False, "leftAt": left_at, "durationMs": duration_ms}},
        )

    def GetGuildSettings(self, guild_id: str) -> GuildSettings | None:
        return self.get_guild_settings(None, guild_id)

    def UpsertGuildSettings(self, settings: GuildSettings | None) -> None:
        self.upsert_guild_settings(None, settings)

    def getAutoRole(self, guild_id: str) -> str:
        return self.get_autorole(None, guild_id)

    def setAutoRole(self, guild_id: str, role_id: str) -> str:
        return self.set_autorole(None, guild_id, role_id)

    def GetAutoRole(self, guild_id: str) -> str:
        return self.get_autorole(None, guild_id)

    def SetAutoRole(self, guild_id: str, role_id: str) -> str:
        return self.set_autorole(None, guild_id, role_id)

    def ClaimMessage(self, subject: str, message_id: str, issuer: str, issued_at: int) -> bool:
        return self.claim_message(None, subject, message_id, issuer, issued_at)

    def ListClosedSessionsPendingNotification(self) -> list[Session]:
        return self.list_closed_sessions_pending_notification(None)

    def ListClosedSessionsPendingSummary(self) -> list[Session]:
        return self.list_closed_sessions_pending_summary(None)

    def ListClosedSessionsByGuildChannel(self, guild_id: str, channel_id: str, limit: int) -> list[Session]:
        return self.list_closed_sessions_by_guild_channel(None, guild_id, channel_id, limit)

    def ListSummariesPendingDelivery(self) -> list[Session]:
        return self.list_summaries_pending_delivery(None)

    def CreateSession(self, session: Session) -> None:
        self.create_session(None, session)

    def FindActiveSession(self, guild_id: str, channel_id: str) -> Session | None:
        return self.find_active_session(None, guild_id, channel_id)

    def ListActiveSessions(self) -> list[Session]:
        return self.list_active_sessions(None)

    def ListActiveSessionsByGuild(self, guild_id: str) -> list[Session]:
        return self.list_active_sessions_by_guild(None, guild_id)

    def GetSessionByID(self, session_id: str) -> Session | None:
        return self.get_session_by_id(None, session_id)

    def CloseSession(self, session_id: str, ended_at: datetime, ended_by_user_id: str) -> None:
        self.close_session(None, session_id, ended_at, ended_by_user_id)

    def MarkSessionClosedEventPublished(self, session_id: str, published_at: datetime) -> None:
        self.mark_session_closed_event_published(None, session_id, published_at)

    def MarkSessionSummaryReady(
        self,
        session_id: str,
        summary_channel_id: str,
        summary_message: str,
        generated_at: datetime,
    ) -> None:
        self.mark_session_summary_ready(None, session_id, summary_channel_id, summary_message, generated_at)

    def ClaimSessionSummaryDelivery(self, session_id: str, claimed_at: datetime) -> bool:
        return self.claim_session_summary_delivery(None, session_id, claimed_at)

    def ReleaseSessionSummaryDeliveryClaim(self, session_id: str) -> None:
        self.release_session_summary_delivery_claim(None, session_id)

    def MarkSessionSummaryDelivered(self, session_id: str, delivered_at: datetime) -> None:
        self.mark_session_summary_delivered(None, session_id, delivered_at)

    def CreateParticipant(self, participant: ParticipantInterval) -> None:
        self.create_participant(None, participant)

    def FindActiveParticipant(self, session_id: str, user_id: str) -> ParticipantInterval | None:
        return self.find_active_participant(None, session_id, user_id)

    def ListActiveParticipants(self, session_id: str) -> list[ParticipantInterval]:
        return self.list_active_participants(None, session_id)

    def ListActiveParticipantsByGuildSession(self, guild_id: str, session_id: str) -> list[ParticipantInterval]:
        return self.list_active_participants_by_guild_session(None, guild_id, session_id)

    def ListParticipantsBySession(self, session_id: str) -> list[ParticipantInterval]:
        return self.list_participants_by_session(None, session_id)

    def ListParticipantsByGuildChannelSession(
        self,
        guild_id: str,
        channel_id: str,
        session_id: str,
    ) -> list[ParticipantInterval]:
        return self.list_participants_by_guild_channel_session(None, guild_id, channel_id, session_id)

    def CloseParticipant(self, participant_id: str, left_at: datetime, duration_ms: int) -> None:
        self.close_participant(None, participant_id, left_at, duration_ms)
