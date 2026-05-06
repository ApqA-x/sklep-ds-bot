from __future__ import annotations

import asyncio
import json
import logging
import warnings
from datetime import UTC, datetime

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="'audioop' is deprecated and slated for removal in Python 3.13",
        category=DeprecationWarning,
    )
    import discord
from nats.aio.client import Client as NATS
from pymongo import MongoClient

from voice_tracker import domain
from voice_tracker.bus import Bus
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _event_enabled(repo: Repository, guild_id: str, event_type: str) -> bool:
    settings = repo.get_guild_settings(None, guild_id)
    if settings is None:
        settings = domain.GuildSettings(guild_id=guild_id)
    enabled = set(domain.clean_activity_event_types(getattr(settings, "activity_event_types", [])))
    return event_type in enabled


def _activity_channel_id(repo: Repository, guild_id: str) -> str:
    settings = repo.get_guild_settings(None, guild_id)
    if settings is None:
        return ""
    return str(getattr(settings, "activity_channel_id", "") or "").strip()


def _event_label(event_type: str) -> str:
    return {
        domain.ACTIVITY_EVENT_MEMBER_JOIN: "Member Joined",
        domain.ACTIVITY_EVENT_MEMBER_LEAVE: "Member Left",
        domain.ACTIVITY_EVENT_INVITE_CREATE: "Invite Created",
        domain.ACTIVITY_EVENT_INVITE_DELETE: "Invite Deleted",
        domain.ACTIVITY_EVENT_INVITE_USED: "Invite Used",
    }.get(event_type, "Activity Event")


def _event_color(event_type: str) -> int:
    return {
        domain.ACTIVITY_EVENT_MEMBER_JOIN: 0x2ECC71,
        domain.ACTIVITY_EVENT_MEMBER_LEAVE: 0xE74C3C,
        domain.ACTIVITY_EVENT_INVITE_CREATE: 0x3498DB,
        domain.ACTIVITY_EVENT_INVITE_DELETE: 0xE67E22,
        domain.ACTIVITY_EVENT_INVITE_USED: 0x9B59B6,
    }.get(event_type, 0x5865F2)


def _member_label(event: domain.ActivityEvent) -> str:
    if event.member_user_id and event.member_name:
        return f"{event.member_name} ({event.member_user_id})"
    if event.member_user_id:
        return event.member_user_id
    if event.member_name:
        return event.member_name
    return "unknown"


def _actor_label(event: domain.ActivityEvent) -> str:
    if event.actor_user_id and event.actor_name:
        return f"{event.actor_name} ({event.actor_user_id})"
    if event.actor_user_id:
        return event.actor_user_id
    if event.actor_name:
        return event.actor_name
    return "unknown"


def _embed_description(event: domain.ActivityEvent) -> str:
    if event.event_type == domain.ACTIVITY_EVENT_MEMBER_JOIN:
        return f"Member: {_member_label(event)}"
    if event.event_type == domain.ACTIVITY_EVENT_MEMBER_LEAVE:
        return f"Member: {_member_label(event)}"
    if event.event_type == domain.ACTIVITY_EVENT_INVITE_CREATE:
        return (
            f"Code: `{event.invite_code or 'unknown'}`\n"
            f"URL: {event.invite_url or 'unknown'}\n"
            f"Created by: {_actor_label(event)}"
        )
    if event.event_type == domain.ACTIVITY_EVENT_INVITE_DELETE:
        return (
            f"Code: `{event.invite_code or 'unknown'}`\n"
            f"URL: {event.invite_url or 'unknown'}\n"
            f"Deleted invite actor: {_actor_label(event)}"
        )
    if event.event_type == domain.ACTIVITY_EVENT_INVITE_USED:
        status = event.attribution_status or "unknown"
        if status == domain.INVITE_ATTRIBUTION_STATUS_EXACT:
            return (
                f"Member: {_member_label(event)}\n"
                f"Code: `{event.invite_code or 'unknown'}`\n"
                f"URL: {event.invite_url or 'unknown'}\n"
                f"Inviter: {_actor_label(event)}\n"
                f"Attribution: {status}"
            )
        return f"Member: {_member_label(event)}\nAttribution: {status}"
    return json.dumps(event.to_dict(), ensure_ascii=False)


def _build_embed(event: domain.ActivityEvent) -> discord.Embed:
    embed = discord.Embed(
        title=_event_label(event.event_type),
        description=_embed_description(event),
        color=_event_color(event.event_type),
        timestamp=event.occurred_at or _utc_now(),
    )
    embed.set_footer(text="Voice Tracker Activity")
    return embed


async def _resolve_channel(client: discord.Client, channel_id: str):
    snowflake = int(channel_id)
    channel = client.get_channel(snowflake)
    if channel is None:
        channel = await client.fetch_channel(snowflake)
    return channel


async def _send_activity(client: discord.Client, channel_id: str, embed: discord.Embed) -> None:
    channel = await _resolve_channel(client, channel_id)
    await channel.send(embed=embed)


async def main() -> None:
    configure_logging("activity")
    cfg = load_config()
    if cfg.discord_token == "":
        raise SystemExit("DISCORD_TOKEN is required")
    if cfg.discord_guild_id == "":
        raise SystemExit("DISCORD_GUILD_ID is required")
    require_event_signing_secret(cfg.event_signing_secret)
    logger.info("activity service starting guild=%s", cfg.discord_guild_id)

    mongo_client = MongoClient(cfg.mongo_uri)
    repo = Repository(mongo_client[cfg.mongo_db])
    repo.ensure_indexes(None)

    nats = NATS()
    await nats.connect(cfg.nats_url)
    bus = Bus(nats, cfg.event_signing_secret, "activity")

    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)

    async def handle_activity(payload: bytes) -> None:
        try:
            body = json.loads(payload.decode("utf-8"))
            event = domain.ActivityEvent.from_dict(body)
        except Exception:
            logger.exception("invalid activity payload")
            return
        if event.guild_id != cfg.discord_guild_id:
            return
        if event.event_type not in domain.ACTIVITY_EVENT_TYPES:
            return
        channel_id = _activity_channel_id(repo, event.guild_id)
        if channel_id == "":
            return
        if not _event_enabled(repo, event.guild_id, event.event_type):
            return
        embed = _build_embed(event)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await _send_activity(client, channel_id, embed)
                return
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.25 * (attempt + 1))
        if last_error is not None:
            logger.exception(
                "activity send failed guild=%s channel=%s event=%s",
                event.guild_id,
                channel_id,
                event.event_type,
                exc_info=last_error,
            )

    await bus.subscribe(None, domain.SUBJECT_ACTIVITY_EVENT, repo, handle_activity)
    await client.login(cfg.discord_token)
    try:
        await client.connect()
    finally:
        await client.close()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
