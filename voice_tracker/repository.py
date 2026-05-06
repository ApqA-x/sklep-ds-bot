from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .domain import (
    INVITE_SOURCE_LIVE_DIFF,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_CLOSED,
    GuildSettings,
    GuildInviteSnapshot,
    InviteCatalogEntry,
    MemberJoinAttribution,
    MemberJoinState,
    MemberRoleState,
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


def _millis_between(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    duration_ms = int((end - start).total_seconds() * 1000)
    if duration_ms < 0:
        return 0
    return duration_ms


def _time_or_min(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value


class Repository:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.guild_settings = _collection(db, "guild_settings")
        self.messages = _collection(db, "processed_messages")
        self.sessions = _collection(db, "voice_sessions")
        self.participants = _collection(db, "voice_session_participants")
        self.guild_invite_snapshots = _collection(db, "guild_invite_snapshots")
        self.invite_catalog = _collection(db, "invite_catalog")
        self.member_join_attributions = _collection(db, "member_join_attributions")
        self.member_join_state = _collection(db, "member_join_state")
        self.member_role_state = _collection(db, "member_role_state")

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
        self.participants.create_index([("guildId", 1), ("userId", 1), ("sessionId", 1)])
        self.participants.create_index([("guildId", 1), ("userId", 1), ("joinedAt", -1)])
        self.participants.create_index(
            [("sessionId", 1), ("userId", 1), ("active", 1)],
            unique=True,
            partialFilterExpression={"active": True},
        )

        self.messages.create_index([("subject", 1), ("messageId", 1)], unique=True)
        self.messages.create_index([("createdAt", 1)], expireAfterSeconds=7200)

        self.guild_invite_snapshots.create_index([("guildId", 1)], unique=True)
        self.guild_invite_snapshots.create_index([("capturedAt", -1)])

        self.invite_catalog.create_index([("guildId", 1), ("code", 1)], unique=True)
        self.invite_catalog.create_index([("guildId", 1), ("lastSeenAt", -1)])
        self.invite_catalog.create_index([("guildId", 1), ("deletedAt", 1)])

        self.member_join_attributions.create_index([("guildId", 1), ("userId", 1), ("joinedAt", -1)])
        self.member_join_attributions.create_index([("guildId", 1), ("joinedAt", -1)])
        self.member_join_attributions.create_index([("guildId", 1), ("attributionStatus", 1), ("joinedAt", -1)])

        self.member_join_state.create_index([("guildId", 1), ("userId", 1)], unique=True)
        self.member_join_state.create_index([("guildId", 1), ("joinedAt", -1)])

        self.member_role_state.create_index([("guildId", 1), ("userId", 1)], unique=True)
        self.member_role_state.create_index([("guildId", 1), ("updatedAt", -1)])
        self.member_role_state.create_index([("guildId", 1), ("lastSeenAt", -1)])
        self.member_role_state.create_index([("guildId", 1), ("pendingRestore", 1), ("updatedAt", -1)])

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
                    "soundboardEnforcementEnabled": settings.soundboard_enforcement_enabled,
                    "managedVoiceChannelId": settings.managed_voice_channel_id,
                    "managedVoiceConnectedAt": settings.managed_voice_connected_at,
                    "inviteSnapshotSyncEnabled": settings.invite_snapshot_sync_enabled,
                    "inviteLiveAttributionEnabled": settings.invite_live_attribution_enabled,
                    "inviteUserinfoEnabled": settings.invite_userinfo_enabled,
                    "inviteReconciliationEnabled": settings.invite_reconciliation_enabled,
                    "activityChannelId": settings.activity_channel_id,
                    "activityEventTypes": list(settings.activity_event_types),
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

    def get_guild_invite_snapshot(self, _ctx: Any, guild_id: str) -> GuildInviteSnapshot | None:
        guild_id = str(guild_id or "").strip()
        if guild_id == "":
            return None
        return GuildInviteSnapshot.from_mongo(self.guild_invite_snapshots.find_one({"_id": guild_id}))

    def upsert_guild_invite_snapshot(self, _ctx: Any, snapshot: GuildInviteSnapshot | None) -> None:
        if snapshot is None or snapshot.guild_id == "":
            return
        if snapshot.captured_at is None:
            snapshot.captured_at = _utc_now()
        payload = snapshot.to_mongo()
        self.guild_invite_snapshots.update_one(
            {"_id": snapshot.guild_id},
            {
                "$set": {
                    "guildId": payload["guildId"],
                    "capturedAt": payload["capturedAt"],
                    "invites": payload["invites"],
                }
            },
            upsert=True,
        )

    def get_invite_catalog_entry(self, _ctx: Any, guild_id: str, code: str) -> InviteCatalogEntry | None:
        guild_id = str(guild_id or "").strip()
        code = str(code or "").strip()
        if guild_id == "" or code == "":
            return None
        return InviteCatalogEntry.from_mongo(self.invite_catalog.find_one({"_id": f"{guild_id}:{code}"}))

    def upsert_invite_catalog_entry(self, _ctx: Any, entry: InviteCatalogEntry | None) -> InviteCatalogEntry | None:
        if entry is None or entry.id == "" or entry.guild_id == "" or entry.code == "":
            return None
        merged = self._merge_invite_catalog_entry(
            InviteCatalogEntry.from_mongo(self.invite_catalog.find_one({"_id": entry.id})),
            entry,
        )
        payload = merged.to_mongo()
        self.invite_catalog.update_one(
            {"_id": merged.id},
            {"$set": {key: value for key, value in payload.items() if key != "_id"}},
            upsert=True,
        )
        return merged

    def sync_invite_catalog_from_snapshot(
        self,
        ctx: Any,
        snapshot: GuildInviteSnapshot | None,
        source: str = "snapshot",
    ) -> list[InviteCatalogEntry]:
        if snapshot is None or snapshot.guild_id == "":
            return []
        last_seen_at = snapshot.captured_at or _utc_now()
        rows: list[InviteCatalogEntry] = []
        for invite in snapshot.invites:
            merged = self.upsert_invite_catalog_entry(
                ctx,
                InviteCatalogEntry(
                    guild_id=snapshot.guild_id,
                    code=invite.code,
                    url=invite.url,
                    channel_id=invite.channel_id,
                    invite_type=invite.invite_type,
                    created_by_user_id=invite.inviter_user_id,
                    created_by_name=invite.inviter_name,
                    last_seen_at=last_seen_at,
                    source=source,
                ),
            )
            if merged is not None:
                rows.append(merged)
        return rows

    def mark_invite_catalog_deleted(
        self,
        _ctx: Any,
        guild_id: str,
        code: str,
        deleted_at: datetime | None,
        source: str = "",
    ) -> InviteCatalogEntry | None:
        current = self.get_invite_catalog_entry(None, guild_id, code)
        if current is None:
            current = InviteCatalogEntry(guild_id=guild_id, code=code)
        if current.id == "":
            return None
        current.deleted_at = deleted_at or _utc_now()
        if source.strip():
            current.source = source
        payload = current.to_mongo()
        self.invite_catalog.update_one(
            {"_id": current.id},
            {"$set": {key: value for key, value in payload.items() if key != "_id"}},
            upsert=True,
        )
        return current

    def append_member_join_attribution(self, _ctx: Any, attribution: MemberJoinAttribution | None) -> bool:
        if attribution is None or attribution.id == "" or attribution.guild_id == "" or attribution.user_id == "":
            return False
        if attribution.joined_at is None:
            return False
        if attribution.created_at is None:
            attribution.created_at = _utc_now()
        try:
            self.member_join_attributions.insert_one(attribution.to_mongo())
        except Exception as err:
            if _is_duplicate_key_error(err):
                return False
            raise
        return True

    def get_member_join_state(self, _ctx: Any, guild_id: str, user_id: str) -> MemberJoinState | None:
        guild_id = str(guild_id or "").strip()
        user_id = str(user_id or "").strip()
        if guild_id == "" or user_id == "":
            return None
        return MemberJoinState.from_mongo(self.member_join_state.find_one({"_id": f"{guild_id}:{user_id}"}))

    def project_member_join_state(self, _ctx: Any, attribution: MemberJoinAttribution | None) -> MemberJoinState | None:
        if attribution is None or attribution.id == "" or attribution.guild_id == "" or attribution.user_id == "":
            return None
        if attribution.source != INVITE_SOURCE_LIVE_DIFF or attribution.joined_at is None:
            return None
        current = self.get_member_join_state(None, attribution.guild_id, attribution.user_id)
        if current is not None:
            current_joined_at = _time_or_min(current.joined_at)
            candidate_joined_at = _time_or_min(attribution.joined_at)
            if current_joined_at > candidate_joined_at:
                return current
            if (
                current_joined_at == candidate_joined_at
                and current.latest_join_attribution_id == attribution.id
                and current.invite_code == attribution.invite_code
                and current.invite_url == attribution.invite_url
                and current.invite_type == attribution.invite_type
                and current.inviter_user_id == attribution.inviter_user_id
                and current.inviter_name == attribution.inviter_name
                and current.attribution_status == attribution.attribution_status
            ):
                return current
            if current_joined_at == candidate_joined_at and current.latest_join_attribution_id not in {"", attribution.id}:
                return current
        state = MemberJoinState(
            guild_id=attribution.guild_id,
            user_id=attribution.user_id,
            latest_join_attribution_id=attribution.id,
            joined_at=attribution.joined_at,
            invite_code=attribution.invite_code,
            invite_url=attribution.invite_url,
            invite_type=attribution.invite_type,
            inviter_user_id=attribution.inviter_user_id,
            inviter_name=attribution.inviter_name,
            attribution_status=attribution.attribution_status,
            updated_at=_utc_now(),
        )
        payload = state.to_mongo()
        self.member_join_state.update_one(
            {"_id": state.id},
            {"$set": {key: value for key, value in payload.items() if key != "_id"}},
            upsert=True,
        )
        return state

    def get_member_role_state(self, _ctx: Any, guild_id: str, user_id: str) -> MemberRoleState | None:
        guild_id = str(guild_id or "").strip()
        user_id = str(user_id or "").strip()
        if guild_id == "" or user_id == "":
            return None
        return MemberRoleState.from_mongo(self.member_role_state.find_one({"_id": f"{guild_id}:{user_id}"}))

    def upsert_member_role_state(self, _ctx: Any, state: MemberRoleState | None) -> MemberRoleState | None:
        if state is None or state.id == "" or state.guild_id == "" or state.user_id == "":
            return None
        if state.updated_at is None:
            state.updated_at = _utc_now()
        payload = state.to_mongo()
        self.member_role_state.update_one(
            {"_id": state.id},
            {
                "$set": {
                    "guildId": payload["guildId"],
                    "userId": payload["userId"],
                    "roleIds": payload["roleIds"],
                    "updatedAt": payload["updatedAt"],
                    "lastSeenAt": payload.get("lastSeenAt"),
                    "lastRestoredAt": payload.get("lastRestoredAt"),
                    "pendingRestore": bool(payload.get("pendingRestore", False)),
                }
            },
            upsert=True,
        )
        return state

    def save_member_role_snapshot(
        self,
        _ctx: Any,
        guild_id: str,
        user_id: str,
        role_ids: list[str] | tuple[str, ...],
        seen_at: datetime | None = None,
        *,
        pending_restore: bool = True,
    ) -> MemberRoleState | None:
        return self.upsert_member_role_state(
            None,
            MemberRoleState(
                guild_id=guild_id,
                user_id=user_id,
                role_ids=list(role_ids or []),
                last_seen_at=seen_at or _utc_now(),
                updated_at=_utc_now(),
                pending_restore=pending_restore,
            ),
        )

    def mark_member_roles_restored(
        self,
        _ctx: Any,
        guild_id: str,
        user_id: str,
        role_ids: list[str] | tuple[str, ...],
        restored_at: datetime | None = None,
    ) -> MemberRoleState | None:
        current = self.get_member_role_state(None, guild_id, user_id)
        return self.upsert_member_role_state(
            None,
            MemberRoleState(
                guild_id=guild_id,
                user_id=user_id,
                role_ids=list(role_ids or []),
                last_seen_at=getattr(current, "last_seen_at", None),
                updated_at=_utc_now(),
                last_restored_at=restored_at or _utc_now(),
                pending_restore=False,
            ),
        )

    def mark_member_roles_pending(self, _ctx: Any, guild_id: str, user_id: str) -> MemberRoleState | None:
        current = self.get_member_role_state(None, guild_id, user_id)
        if current is None:
            return None
        return self.upsert_member_role_state(
            None,
            MemberRoleState(
                guild_id=guild_id,
                user_id=user_id,
                role_ids=list(getattr(current, "role_ids", []) or []),
                last_seen_at=getattr(current, "last_seen_at", None),
                updated_at=_utc_now(),
                last_restored_at=getattr(current, "last_restored_at", None),
                pending_restore=True,
            ),
        )

    def list_member_role_states_by_guild(
        self,
        _ctx: Any,
        guild_id: str,
        limit: int = 0,
        *,
        pending_restore_only: bool = False,
    ) -> list[MemberRoleState]:
        guild_id = str(guild_id or "").strip()
        if guild_id == "":
            return []
        query: dict[str, Any] = {"guildId": guild_id}
        if pending_restore_only:
            query["pendingRestore"] = True
        cursor = self.member_role_state.find(query).sort([("updatedAt", -1)])
        if int(limit or 0) > 0:
            cursor = cursor.limit(max(1, int(limit)))
        return [item for item in (MemberRoleState.from_mongo(doc) for doc in _cursor_all(cursor)) if item is not None]

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

    def list_voice_totals_by_guild(self, _ctx: Any, guild_id: str) -> list[dict[str, Any]]:
        guild_id = str(guild_id or "").strip()
        if guild_id == "":
            return []

        participants = [
            p
            for p in (
                ParticipantInterval.from_mongo(doc)
                for doc in _cursor_all(self.participants.find({"guildId": guild_id}))
            )
            if p is not None and str(p.user_id or "").strip() != ""
        ]
        if len(participants) == 0:
            return []
        participants.sort(key=lambda item: (str(item.user_id or ""), _time_or_min(item.joined_at), str(item.id or "")))

        sessions = self._load_sessions_by_ids(guild_id, {str(p.session_id or "").strip() for p in participants})
        now = _utc_now()
        totals: dict[str, dict[str, Any]] = {}
        name_rank: dict[str, tuple[int, datetime, str]] = {}
        for participant in participants:
            user_id = str(participant.user_id or "").strip()
            if user_id == "":
                continue
            total = totals.get(user_id)
            if total is None:
                total = {"user_id": user_id, "user_name": "", "total_for": 0}
                totals[user_id] = total
            total["total_for"] = int(total["total_for"]) + self._participant_duration_ms(
                participant, sessions.get(str(participant.session_id or "").strip()), now
            )

            user_name = str(participant.user_name or "").strip()
            if user_name != "":
                candidate_rank = (
                    1 if participant.joined_at is not None else 0,
                    _time_or_min(participant.joined_at),
                    str(participant.id or ""),
                )
                if candidate_rank >= name_rank.get(user_id, (0, datetime.min.replace(tzinfo=UTC), "")):
                    total["user_name"] = user_name
                    name_rank[user_id] = candidate_rank

        rows = list(totals.values())
        rows.sort(
            key=lambda row: (
                -int(row["total_for"]),
                str(row["user_name"] or row["user_id"]).casefold(),
                str(row["user_id"]),
            )
        )
        return rows

    def get_member_profile(self, _ctx: Any, guild_id: str, user_id: str) -> dict[str, Any] | None:
        guild_id = str(guild_id or "").strip()
        user_id = str(user_id or "").strip()
        if guild_id == "" or user_id == "":
            return None

        join_state = self.get_member_join_state(None, guild_id, user_id)
        role_state = self.get_member_role_state(None, guild_id, user_id)

        participants = [
            p
            for p in (
                ParticipantInterval.from_mongo(doc)
                for doc in _cursor_all(self.participants.find({"guildId": guild_id, "userId": user_id}))
            )
            if p is not None
        ]
        if len(participants) == 0 and join_state is None and role_state is None:
            return None
        total_for = 0
        user_name = ""
        selected_name_rank = (0, datetime.min.replace(tzinfo=UTC), "")
        if len(participants) > 0:
            participants.sort(key=lambda item: (_time_or_min(item.joined_at), str(item.id or "")))
            sessions = self._load_sessions_by_ids(guild_id, {str(p.session_id or "").strip() for p in participants})
            now = _utc_now()
            for participant in participants:
                total_for += self._participant_duration_ms(
                    participant, sessions.get(str(participant.session_id or "").strip()), now
                )
                candidate_name = str(participant.user_name or "").strip()
                if candidate_name == "":
                    continue
                candidate_rank = (
                    1 if participant.joined_at is not None else 0,
                    _time_or_min(participant.joined_at),
                    str(participant.id or ""),
                )
                if candidate_rank >= selected_name_rank:
                    user_name = candidate_name
                    selected_name_rank = candidate_rank

        profile = {
            "user_id": user_id,
            "user_name": user_name,
            "total_for": total_for,
            "roles": list(getattr(role_state, "role_ids", []) or []),
        }
        if role_state is not None:
            profile.update(
                {
                    "roles_last_seen_at": role_state.last_seen_at,
                    "roles_last_restored_at": role_state.last_restored_at,
                }
            )
        if join_state is not None:
            profile.update(
                {
                    "latest_join_attribution_id": join_state.latest_join_attribution_id,
                    "joined_at": join_state.joined_at,
                    "invite_code": join_state.invite_code,
                    "invite_url": join_state.invite_url,
                    "invite_type": join_state.invite_type,
                    "inviter_user_id": join_state.inviter_user_id,
                    "inviter_name": join_state.inviter_name,
                    "attribution_status": join_state.attribution_status,
                }
            )
        return profile

    def get_user_voice_summary(self, ctx: Any, guild_id: str, user_id: str) -> dict[str, Any] | None:
        return self.get_member_profile(ctx, guild_id, user_id)

    def _load_sessions_by_ids(self, guild_id: str, session_ids: set[str]) -> dict[str, Session]:
        session_ids = {str(session_id or "").strip() for session_id in session_ids if str(session_id or "").strip() != ""}
        if len(session_ids) == 0:
            return {}
        cursor = self.sessions.find({"guildId": guild_id, "_id": {"$in": sorted(session_ids)}})
        sessions: dict[str, Session] = {}
        for session in (Session.from_mongo(doc) for doc in _cursor_all(cursor)):
            if session is None:
                continue
            sessions[session.id] = session
        return sessions

    def _participant_duration_ms(self, participant: ParticipantInterval, session: Session | None, now: datetime) -> int:
        duration_ms = int(participant.duration_ms or 0)
        if duration_ms > 0:
            return duration_ms

        end = participant.left_at
        if end is None and session is not None and session.ended_at is not None:
            end = session.ended_at
        return _millis_between(participant.joined_at, end)

    def _merge_invite_catalog_entry(
        self,
        current: InviteCatalogEntry | None,
        incoming: InviteCatalogEntry,
    ) -> InviteCatalogEntry:
        if current is None:
            return incoming

        created_at = current.created_at
        if incoming.created_at is not None and (created_at is None or incoming.created_at < created_at):
            created_at = incoming.created_at

        deleted_at = current.deleted_at
        if incoming.deleted_at is None and incoming.last_seen_at is not None:
            deleted_at = None
        elif incoming.deleted_at is not None and (deleted_at is None or incoming.deleted_at < deleted_at):
            deleted_at = incoming.deleted_at

        last_seen_at = current.last_seen_at
        if incoming.last_seen_at is not None and (last_seen_at is None or incoming.last_seen_at > last_seen_at):
            last_seen_at = incoming.last_seen_at

        creator_rank_current = self._invite_creator_rank(current.source)
        creator_rank_incoming = self._invite_creator_rank(incoming.source)
        created_by_user_id = current.created_by_user_id
        created_by_name = current.created_by_name
        if incoming.created_by_user_id or incoming.created_by_name:
            if not created_by_user_id and not created_by_name:
                created_by_user_id = incoming.created_by_user_id
                created_by_name = incoming.created_by_name
            elif creator_rank_incoming >= creator_rank_current:
                created_by_user_id = incoming.created_by_user_id or created_by_user_id
                created_by_name = incoming.created_by_name or created_by_name

        return InviteCatalogEntry(
            id=current.id or incoming.id,
            guild_id=current.guild_id or incoming.guild_id,
            code=current.code or incoming.code,
            url=incoming.url or current.url,
            channel_id=incoming.channel_id or current.channel_id,
            invite_type=incoming.invite_type or current.invite_type,
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
            created_at=created_at,
            deleted_at=deleted_at,
            last_seen_at=last_seen_at,
            source=incoming.source or current.source,
        )

    def _invite_creator_rank(self, source: str) -> int:
        return {
            "live_event": 3,
            "audit_log": 2,
            "snapshot": 1,
        }.get(str(source or "").strip(), 0)

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
