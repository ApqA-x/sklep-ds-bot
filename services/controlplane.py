from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from os import environ
from typing import Any
import warnings
from uuid import uuid4

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="'audioop' is deprecated and slated for removal in Python 3.13",
        category=DeprecationWarning,
    )
    import discord
from nats.aio.client import Client as NATS
from pymongo import MongoClient

from voice_tracker.bus import decode_envelope, sign_envelope
from voice_tracker import domain
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret


logger = logging.getLogger(__name__)

ACTIVITY_EVENTS_FULL = sorted(domain.ACTIVITY_EVENT_TYPES)
ACTIVITY_EVENTS_MINIMAL = sorted(
    {
        domain.ACTIVITY_EVENT_MEMBER_JOIN,
        domain.ACTIVITY_EVENT_MEMBER_LEAVE,
        domain.ACTIVITY_EVENT_INVITE_USED,
        domain.ACTIVITY_EVENT_VOICE_JOIN,
        domain.ACTIVITY_EVENT_VOICE_LEAVE,
        domain.ACTIVITY_EVENT_VOICE_MOVE,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _subject(name: str, fallback: str) -> str:
    return str(environ.get(name, fallback) or fallback).strip()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _guild_int(guild_id: Any) -> int:
    try:
        return int(str(guild_id).strip())
    except (TypeError, ValueError):
        return 0


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        cleaned = _clean(item)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return sorted(result)


def _activity_events(config: dict[str, Any]) -> list[str]:
    explicit = _clean_list(config.get("activityEventTypes"))
    if explicit:
        return sorted(event for event in explicit if event in domain.ACTIVITY_EVENT_TYPES)
    mode = _clean(config.get("activityMode") or "full").lower()
    if mode == "off":
        return []
    if mode == "minimal":
        return ACTIVITY_EVENTS_MINIMAL
    return ACTIVITY_EVENTS_FULL


def _event_payload(event_type: str, guild_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"bot:{event_type}:{guild_id}:{int(_utc_now().timestamp() * 1000)}",
        "guild_id": guild_id,
        "event_type": event_type,
        "payload": payload,
        "created_at": _utc_now(),
    }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ControlPlane:
    def __init__(self, mongo_client: MongoClient, nats: NATS, client: discord.Client | None = None) -> None:
        cfg = load_config()
        self.cfg = cfg
        self.mongo_client = mongo_client
        self.db = mongo_client[cfg.mongo_db]
        self.repo = Repository(self.db)
        self.nats = nats
        self.client = client
        self.apply_subject = _subject("DASHBOARD_APPLY_SUBJECT", "dashboard.apply.settings")
        self.ops_subject = _subject("DASHBOARD_OPS_SUBJECT", "dashboard.ops.dispatch")
        self.apply_result_subject = _subject("DASHBOARD_APPLY_RESULT_SUBJECT", "dashboard.apply.result")
        self.ops_result_subject = _subject("DASHBOARD_OPS_RESULT_SUBJECT", "dashboard.ops.result")
        self.event_signing_secret = require_event_signing_secret(cfg.event_signing_secret)
        self.ready = asyncio.Event()

    async def start(self) -> None:
        self.repo.ensure_indexes(None)
        await self.nats.subscribe(self.apply_subject, cb=self._handle_apply_msg)
        await self.nats.subscribe(self.ops_subject, cb=self._handle_ops_msg)
        await self._heartbeat_loop_once()
        logger.info("controlplane subscribed apply=%s ops=%s", self.apply_subject, self.ops_subject)

    async def heartbeat_loop(self) -> None:
        while True:
            await self._heartbeat_loop_once()
            await asyncio.sleep(15)

    async def _heartbeat_loop_once(self) -> None:
        self.db.bot_runtime_heartbeats.replace_one(
            {"worker": "dsbot-controlplane"},
            {"worker": "dsbot-controlplane", "updated_at": _utc_now()},
            upsert=True,
        )

    async def _handle_apply_msg(self, msg: Any) -> None:
        try:
            env, payload = decode_envelope(self.event_signing_secret, self.apply_subject, msg.data)
            if env.issuer != "dashboard":
                raise ValueError(f'unexpected issuer "{env.issuer}"')
            event = json.loads(payload.decode("utf-8"))
            await self.apply_settings(event)
        except ValueError as exc:
            logger.warning("apply message rejected: %s", exc)
        except Exception:
            logger.exception("apply message failed")

    async def _handle_ops_msg(self, msg: Any) -> None:
        try:
            env, payload = decode_envelope(self.event_signing_secret, self.ops_subject, msg.data)
            if env.issuer != "dashboard":
                raise ValueError(f'unexpected issuer "{env.issuer}"')
            event = json.loads(payload.decode("utf-8"))
            await self.execute_operation(event)
        except ValueError as exc:
            logger.warning("operation message rejected: %s", exc)
        except Exception:
            logger.exception("operation message failed")

    async def apply_settings(self, event: dict[str, Any]) -> None:
        guild_id = _guild_int(event.get("guild_id"))
        job_id = _clean(event.get("apply_job_id"))
        module_key = _clean(event.get("module_key"))
        if guild_id == 0 or not job_id:
            return

        try:
            module_doc = self.db.web_module_configs.find_one({"guild_id": guild_id, "module_key": module_key}, {"_id": 0})
            config = module_doc.get("config", {}) if isinstance(module_doc, dict) else {}
            if not isinstance(config, dict):
                config = {}
            if module_key in {"common", "voice/settings", "voice/access"}:
                self._apply_voice_config(str(guild_id), config)
            self._cache_module(guild_id, module_key, config)
            self._mark_job(job_id, "applied")
            self._runtime_event("settings_applied", guild_id, {"module_key": module_key, "apply_job_id": job_id})
            await self._publish_result(self.apply_result_subject, {"ok": True, **event})
        except Exception as exc:
            self._mark_job(job_id, "failed", str(exc))
            self._runtime_event("settings_apply_failed", guild_id, {"module_key": module_key, "apply_job_id": job_id, "error": str(exc)})
            await self._publish_result(self.apply_result_subject, {"ok": False, "error": str(exc), **event})
            raise

    async def execute_operation(self, event: dict[str, Any]) -> None:
        guild_id = _guild_int(event.get("guild_id"))
        job_id = _clean(event.get("apply_job_id"))
        operation = _clean(event.get("operation"))
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if guild_id == 0 or not job_id:
            return
        try:
            result = await self._execute_operation(guild_id, operation, payload)
            self._mark_job(job_id, "applied")
            self._runtime_status(guild_id, "active", {"last_operation": operation, "result": result})
            self._runtime_event("operation_applied", guild_id, {"operation": operation, "apply_job_id": job_id, "result": result})
            await self._publish_result(self.ops_result_subject, {"ok": True, "result": result, **event})
        except Exception as exc:
            self._mark_job(job_id, "failed", str(exc))
            self._runtime_status(guild_id, "error", {"last_operation": operation, "error": str(exc)})
            self._runtime_event("operation_failed", guild_id, {"operation": operation, "apply_job_id": job_id, "error": str(exc)})
            await self._publish_result(self.ops_result_subject, {"ok": False, "error": str(exc), **event})
            raise

    def _apply_voice_config(self, guild_id: str, config: dict[str, Any]) -> None:
        settings = self.repo.get_guild_settings(None, guild_id) or domain.GuildSettings(guild_id=guild_id)
        summary_channel_id = _clean(config.get("summaryChannelId"))
        activity_channel_id = _clean(config.get("activityChannelId"))
        auto_role_id = _clean(config.get("autoRoleId") or config.get("startRoleId"))
        managed_voice_channel_id = _clean(config.get("managedVoiceChannelId"))

        settings.summary_channel_id = summary_channel_id
        settings.activity_channel_id = activity_channel_id
        settings.auto_role_id = auto_role_id
        settings.auto_unmute_user_ids = _clean_list(config.get("autoUnmuteUserIds"))
        settings.trusted_user_ids = _clean_list(config.get("trustedUserIds"))
        settings.soundboard_enforcement_enabled = bool(config.get("soundboardEnforcementEnabled", False))
        settings.managed_voice_channel_id = managed_voice_channel_id
        settings.invite_snapshot_sync_enabled = bool(config.get("inviteSnapshotSyncEnabled", True))
        settings.invite_live_attribution_enabled = bool(config.get("inviteLiveAttributionEnabled", True))
        settings.invite_userinfo_enabled = bool(config.get("inviteUserinfoEnabled", True))
        settings.invite_reconciliation_enabled = bool(config.get("inviteReconciliationEnabled", False))
        settings.activity_event_types = _activity_events(config)
        self.repo.upsert_guild_settings(None, settings)

    async def _execute_operation(self, guild_id: int, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        guild_key = str(guild_id)
        if operation == "status":
            return {"status": "online", "worker": "dsbot-controlplane"}
        if operation == "connect":
            channel_id = _clean(payload.get("channel_id"))
            if not channel_id:
                raise ValueError("channel_id is required")
            settings = self.repo.get_guild_settings(None, guild_key) or domain.GuildSettings(guild_id=guild_key)
            settings.managed_voice_channel_id = channel_id
            self.repo.upsert_guild_settings(None, settings)
            return {"managedVoiceChannelId": channel_id}
        if operation == "disconnect":
            settings = self.repo.get_guild_settings(None, guild_key) or domain.GuildSettings(guild_id=guild_key)
            settings.managed_voice_channel_id = ""
            settings.managed_voice_connected_at = None
            self.repo.upsert_guild_settings(None, settings)
            return {"managedVoiceChannelId": None}
        if operation == "trusted":
            member_id = _clean(payload.get("member_id"))
            if not member_id:
                raise ValueError("member_id is required")
            trusted = bool(payload.get("trusted", True))
            ids = self.repo.add_trusted_user(None, guild_key, member_id) if trusted else self.repo.remove_trusted_user(None, guild_key, member_id)
            self._runtime_flag(guild_id, "trusted", trusted, {"member_id": member_id})
            return {"trustedUserIds": ids}
        if operation == "stalker":
            member_id = _clean(payload.get("member_id"))
            enabled = bool(payload.get("enabled", True))
            if not member_id:
                raise ValueError("member_id is required")
            self._runtime_flag(guild_id, "stalker", enabled, {"member_id": member_id})
            return {"member_id": member_id, "enabled": enabled}
        if operation == "unmute":
            member_id = _clean(payload.get("member_id"))
            if not member_id:
                raise ValueError("member_id is required")
            ids = self.repo.add_auto_unmute_user(None, guild_key, member_id)
            await self._edit_member_voice(guild_id, member_id, mute=False, deafen=False)
            self._runtime_flag(guild_id, "auto_unmute", True, {"member_id": member_id})
            return {"autoUnmuteUserIds": ids}
        if operation == "autorole":
            role_id = _clean(payload.get("role_id"))
            member_id = _clean(payload.get("member_id"))
            if not role_id:
                raise ValueError("role_id is required")
            settings = self.repo.get_guild_settings(None, guild_key) or domain.GuildSettings(guild_id=guild_key)
            settings.auto_role_id = role_id
            self.repo.upsert_guild_settings(None, settings)
            if member_id:
                await self._add_member_role(guild_id, member_id, role_id)
            self._runtime_flag(guild_id, "autorole", True, {"member_id": member_id, "role_id": role_id})
            return {"autoRoleId": role_id, "member_id": member_id or None}
        if operation == "jump":
            member_id = _clean(payload.get("member_id"))
            channel_id = _clean(payload.get("channel_id"))
            if not member_id or not channel_id:
                raise ValueError("member_id and channel_id are required")
            await self._move_member(guild_id, member_id, channel_id)
            return {"member_id": member_id, "channel_id": channel_id}
        if operation == "inspect":
            return self._inspect(guild_id, payload)
        raise ValueError(f"unsupported operation: {operation}")

    async def _guild(self, guild_id: int) -> discord.Guild:
        if self.client is None:
            raise RuntimeError("Discord client is not available")
        await self.ready.wait()
        guild = self.client.get_guild(guild_id)
        if guild is None:
            raise RuntimeError("Guild is not available to the bot")
        return guild

    async def _member(self, guild: discord.Guild, member_id: str) -> discord.Member:
        member = guild.get_member(int(member_id))
        if member is not None:
            return member
        return await guild.fetch_member(int(member_id))

    async def _move_member(self, guild_id: int, member_id: str, channel_id: str) -> None:
        guild = await self._guild(guild_id)
        member = await self._member(guild, member_id)
        channel = guild.get_channel(int(channel_id)) or await guild.fetch_channel(int(channel_id))
        await member.move_to(channel, reason="Dashboard operation: jump")

    async def _add_member_role(self, guild_id: int, member_id: str, role_id: str) -> None:
        guild = await self._guild(guild_id)
        member = await self._member(guild, member_id)
        role = guild.get_role(int(role_id))
        if role is None:
            roles = await guild.fetch_roles()
            role = next((item for item in roles if str(item.id) == role_id), None)
        if role is None:
            raise RuntimeError("Role is not available to the bot")
        await member.add_roles(role, reason="Dashboard operation: autorole")

    async def _edit_member_voice(self, guild_id: int, member_id: str, **kwargs: bool) -> None:
        guild = await self._guild(guild_id)
        member = await self._member(guild, member_id)
        await member.edit(reason="Dashboard operation: unmute", **kwargs)

    def _inspect(self, guild_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        channel_id = _clean(payload.get("channel_id"))
        member_id = _clean(payload.get("member_id"))
        if channel_id:
            sessions = list(self.db.voice_sessions.find({"guildId": str(guild_id), "channelId": channel_id, "status": "active"}, {"_id": 0}))
            return {"sessions": sessions}
        if member_id:
            participants = list(self.db.voice_session_participants.find({"guildId": str(guild_id), "userId": member_id}, {"_id": 0}))
            return {"participants": participants[-20:]}
        return {}

    def _cache_module(self, guild_id: int, module_key: str, config: dict[str, Any]) -> None:
        self.db.bot_runtime_module_cache.replace_one(
            {"guild_id": guild_id, "module_key": module_key},
            {
                "guild_id": guild_id,
                "module_key": module_key,
                "config": config,
                "updated_at": _utc_now(),
                "source": "dsbot-controlplane",
            },
            upsert=True,
        )

    def _mark_job(self, job_id: str, state: str, error: str | None = None) -> None:
        payload: dict[str, Any] = {"state": state, "updated_at": _utc_now()}
        if error is not None:
            payload["error"] = error
        self.db.web_apply_jobs.update_one({"id": job_id}, {"$set": payload})

    def _runtime_status(self, guild_id: int, status: str, details: dict[str, Any]) -> None:
        self.db.web_ops_runtime.replace_one(
            {"guild_id": guild_id},
            {"guild_id": guild_id, "status": status, "details": details, "updated_at": _utc_now()},
            upsert=True,
        )

    def _runtime_flag(self, guild_id: int, key: str, value: Any, meta: dict[str, Any]) -> None:
        flag_id = f"{guild_id}:{key}:{json.dumps(meta, sort_keys=True)}"
        self.db.bot_runtime_flags.replace_one(
            {"id": flag_id},
            {"id": flag_id, "guild_id": guild_id, "key": key, "value": value, "meta": meta, "updated_at": _utc_now()},
            upsert=True,
        )

    def _runtime_event(self, event_type: str, guild_id: int, payload: dict[str, Any]) -> None:
        self.db.bot_runtime_events.insert_one(_event_payload(event_type, guild_id, payload))

    async def _publish_result(self, subject: str, payload: dict[str, Any]) -> None:
        issued_at = int(_utc_now().timestamp())
        message_id = str(uuid4())
        issuer = "dsbot-controlplane"
        payload_bytes = _json_bytes(payload)
        envelope = {
            "messageId": message_id,
            "subject": subject,
            "issuer": issuer,
            "issuedAt": issued_at,
            "payload": json.loads(payload_bytes.decode("utf-8")),
            "signature": sign_envelope(self.event_signing_secret, message_id, subject, issuer, issued_at, payload_bytes),
        }
        await self.nats.publish(subject, _json_bytes(envelope))


async def main() -> None:
    configure_logging("controlplane")
    cfg = load_config()
    if cfg.discord_token == "":
        raise SystemExit("DISCORD_TOKEN is required")

    mongo_client = MongoClient(cfg.mongo_uri)
    nats = NATS()
    await nats.connect(cfg.nats_url)

    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.voice_states = True
    client = discord.Client(intents=intents)

    controlplane = ControlPlane(mongo_client, nats, client)

    @client.event
    async def on_ready() -> None:
        controlplane.ready.set()
        logger.info("controlplane discord ready guilds=%s", len(getattr(client, "guilds", []) or []))

    await controlplane.start()
    heartbeat = asyncio.create_task(controlplane.heartbeat_loop(), name="controlplane-heartbeat")
    try:
        await client.start(cfg.discord_token)
    finally:
        heartbeat.cancel()
        await nats.drain()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
