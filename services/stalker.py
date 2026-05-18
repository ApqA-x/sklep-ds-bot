from __future__ import annotations

import asyncio
import json
import logging
import warnings
from datetime import datetime

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="'audioop' is deprecated and slated for removal in Python 3.13",
        category=DeprecationWarning,
    )
    import discord
from nats.aio.client import Client as NATS
from pymongo import MongoClient

from services.chat_templates import stalker_update
from voice_tracker import domain
from voice_tracker.bus import Bus
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret


logger = logging.getLogger(__name__)


class _ServiceDeduper:
    def __init__(self, repo: Repository, namespace: str) -> None:
        self.repo = repo
        self.namespace = namespace.strip() or "stalker"

    def claim_message(self, ctx, subject: str, message_id: str, issuer: str, issued_at: int) -> bool:
        scoped_subject = f"{self.namespace}:{subject}"
        return bool(self.repo.claim_message(ctx, scoped_subject, message_id, issuer, issued_at))


def _channel_label(channel: object | None, channel_id: str) -> str:
    name = str(getattr(channel, "name", "") or "").strip()
    if name != "":
        return name
    if channel_id != "":
        return f"channel {channel_id}"
    return "unknown channel"


def _target_label(user_name: str, user_id: str) -> str:
    clean_name = str(user_name or "").strip()
    clean_user_id = str(user_id or "").strip()
    if clean_name and clean_user_id:
        return f"{clean_name} (<@{clean_user_id}>)"
    if clean_name:
        return clean_name
    if clean_user_id:
        return f"<@{clean_user_id}>"
    return "unknown user"


def _voice_event_message(
    event: domain.VoiceStateEvent,
    guild_name: str,
    previous_channel_label: str,
    current_channel_label: str,
) -> str:
    target = _target_label(event.user_name, event.user_id)
    guild_label = guild_name or event.guild_id or "unknown server"
    if event.previous_channel_id == "" and event.channel_id != "":
        return f"Stalker update: {target} joined voice channel {current_channel_label} in {guild_label}."
    if event.previous_channel_id != "" and event.channel_id == "":
        return f"Stalker update: {target} left voice channel {previous_channel_label} in {guild_label}."
    return (
        f"Stalker update: {target} moved from voice channel {previous_channel_label} "
        f"to {current_channel_label} in {guild_label}."
    )


def _activity_event_message(event: domain.ActivityEvent, guild_name: str) -> str:
    target = _target_label(event.member_name, event.member_user_id)
    guild_label = guild_name or event.guild_id or "unknown server"
    if event.event_type == domain.ACTIVITY_EVENT_MEMBER_JOIN:
        return f"Stalker update: {target} joined {guild_label}."
    return f"Stalker update: {target} left {guild_label}."


def _build_dm_embed(message: str, occurred_at: datetime | None = None) -> discord.Embed:
    payload = stalker_update.render(message=message)
    embed = discord.Embed(
        title=str(payload.get("title", "Voice Tracker")),
        description=str(payload.get("description", message)),
        color=int(payload.get("color", 0x5865F2)),
        timestamp=occurred_at,
    )
    embed.set_footer(text=str(payload.get("footer", "Voice Tracker")))
    return embed


async def _resolve_channel(client: discord.Client, channel_id: str):
    if not str(channel_id or "").strip():
        return None
    snowflake = int(channel_id)
    channel = client.get_channel(snowflake)
    if channel is None:
        channel = await client.fetch_channel(snowflake)
    return channel


async def _resolve_channel_name(client: discord.Client, channel_id: str) -> str:
    if str(channel_id or "").strip() == "":
        return "unknown channel"
    try:
        channel = await _resolve_channel(client, channel_id)
    except Exception:
        logger.warning("stalker channel resolve failed channel=%s", channel_id, exc_info=True)
        return _channel_label(None, channel_id)
    return _channel_label(channel, channel_id)


async def _resolve_user(client: discord.Client, user_id: str):
    snowflake = int(user_id)
    user = client.get_user(snowflake)
    if user is None:
        user = await client.fetch_user(snowflake)
    return user


async def _send_dm(client: discord.Client, watcher_user_id: str, embed: discord.Embed) -> None:
    user = await _resolve_user(client, watcher_user_id)
    await user.send(embed=embed)


async def _deliver_to_watchers(client: discord.Client, watcher_user_ids: list[str], embed: discord.Embed) -> None:
    for watcher_user_id in watcher_user_ids:
        try:
            await _send_dm(client, watcher_user_id, embed)
        except Exception:
            logger.exception("stalker dm failed watcher=%s", watcher_user_id)


def _active_watcher_user_ids(repo: Repository, guild_id: str, subscriptions: list[domain.StalkerSubscription]) -> list[str]:
    trusted_user_ids = set(repo.get_trusted_user_ids(None, guild_id))
    watcher_user_ids: list[str] = []
    seen: set[str] = set()
    for subscription in subscriptions:
        watcher_user_id = str(subscription.watcher_user_id or "").strip()
        if watcher_user_id == "" or watcher_user_id in seen:
            continue
        if watcher_user_id not in trusted_user_ids:
            repo.delete_stalker_subscriptions_by_watcher(None, guild_id, watcher_user_id)
            continue
        seen.add(watcher_user_id)
        watcher_user_ids.append(watcher_user_id)
    return watcher_user_ids


async def main() -> None:
    configure_logging("stalker")
    cfg = load_config()
    if cfg.discord_token == "":
        raise SystemExit("DISCORD_TOKEN is required")
    if cfg.discord_guild_id == "":
        raise SystemExit("DISCORD_GUILD_ID is required")
    require_event_signing_secret(cfg.event_signing_secret)
    logger.info("stalker service starting guild=%s", cfg.discord_guild_id)

    mongo_client = MongoClient(cfg.mongo_uri)
    repo = Repository(mongo_client[cfg.mongo_db])
    repo.ensure_indexes(None)
    deduper = _ServiceDeduper(repo, "stalker")

    nats = NATS()
    await nats.connect(cfg.nats_url)
    bus = Bus(nats, cfg.event_signing_secret, "stalker")

    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)
    ready = asyncio.Event()

    @client.event
    async def on_ready() -> None:
        ready.set()

    async def handle_voice(payload: bytes) -> None:
        await ready.wait()
        try:
            event = domain.VoiceStateEvent.from_dict(json.loads(payload.decode("utf-8")))
        except Exception:
            logger.exception("invalid voice payload")
            return
        if event.guild_id != cfg.discord_guild_id or event.user_id == "" or event.is_bot:
            return
        if event.previous_channel_id == event.channel_id:
            return
        subscriptions = repo.list_stalker_subscriptions_by_target(None, event.guild_id, event.user_id)
        if len(subscriptions) == 0:
            return
        watcher_user_ids = _active_watcher_user_ids(repo, event.guild_id, subscriptions)
        if len(watcher_user_ids) == 0:
            return
        guild = client.get_guild(int(event.guild_id)) if event.guild_id.isdigit() else None
        guild_name = str(getattr(guild, "name", "") or "")
        previous_channel_label = await _resolve_channel_name(client, event.previous_channel_id) if event.previous_channel_id else ""
        current_channel_label = await _resolve_channel_name(client, event.channel_id) if event.channel_id else ""
        message = _voice_event_message(event, guild_name, previous_channel_label, current_channel_label)
        embed = _build_dm_embed(message, event.occurred_at)
        await _deliver_to_watchers(client, [watcher_user_id for watcher_user_id in watcher_user_ids if watcher_user_id != event.user_id], embed)

    async def handle_activity(payload: bytes) -> None:
        await ready.wait()
        try:
            event = domain.ActivityEvent.from_dict(json.loads(payload.decode("utf-8")))
        except Exception:
            logger.exception("invalid activity payload")
            return
        if event.guild_id != cfg.discord_guild_id or event.member_user_id == "":
            return
        if event.event_type not in {domain.ACTIVITY_EVENT_MEMBER_JOIN, domain.ACTIVITY_EVENT_MEMBER_LEAVE}:
            return
        subscriptions = repo.list_stalker_subscriptions_by_target(None, event.guild_id, event.member_user_id)
        if len(subscriptions) == 0:
            return
        watcher_user_ids = _active_watcher_user_ids(repo, event.guild_id, subscriptions)
        if len(watcher_user_ids) == 0:
            return
        guild = client.get_guild(int(event.guild_id)) if event.guild_id.isdigit() else None
        guild_name = str(getattr(guild, "name", "") or "")
        message = _activity_event_message(event, guild_name)
        embed = _build_dm_embed(message, event.occurred_at)
        await _deliver_to_watchers(
            client,
            [watcher_user_id for watcher_user_id in watcher_user_ids if watcher_user_id != event.member_user_id],
            embed,
        )

    await bus.subscribe(None, domain.SUBJECT_VOICE_EVENT, deduper, handle_voice)
    await bus.subscribe(None, domain.SUBJECT_ACTIVITY_EVENT, deduper, handle_activity)
    await client.login(cfg.discord_token)
    try:
        await client.connect()
    finally:
        await client.close()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
