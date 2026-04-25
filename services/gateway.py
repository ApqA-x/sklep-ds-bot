from __future__ import annotations

import asyncio
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

from voice_tracker.bus import Bus
from voice_tracker import domain
from voice_tracker.gateway import Service as GatewayService, summary_from_payload
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret


SUMMARY_EMBED_COLOR = 0x5865F2
logger = logging.getLogger(__name__)


async def _resolve_channel(client: discord.Client, channel_id: str):
    snowflake = int(channel_id)
    channel = client.get_channel(snowflake)
    if channel is None:
        channel = await client.fetch_channel(snowflake)
    return channel


async def _send_summary(client: discord.Client, channel_id: str, message: str) -> None:
    channel = await _resolve_channel(client, channel_id)
    embed = discord.Embed(title="Voice Session Summary", description=message, color=SUMMARY_EMBED_COLOR)
    embed.set_footer(text="Voice Tracker")
    await channel.send(embed=embed)


async def _deliver_pending(client: discord.Client, repo: Repository) -> None:
    for session in repo.list_summaries_pending_delivery(None):
        if not session.summary_channel_id or not session.summary_message:
            continue
        claimed = repo.claim_session_summary_delivery(None, session.id, datetime.now(UTC))
        if not claimed:
            continue
        try:
            await _send_summary(client, session.summary_channel_id, session.summary_message)
        except Exception:
            logger.exception(
                "pending summary delivery failed session_id=%s guild_id=%s channel_id=%s",
                session.id,
                session.guild_id,
                session.summary_channel_id,
            )
            repo.release_session_summary_delivery_claim(None, session.id)
            continue
        repo.mark_session_summary_delivered(None, session.id, datetime.now(UTC))


def _autorole_id_for_guild(repo: Repository, guild_id: str) -> str:
    settings = None
    getter = getattr(repo, "get_guild_settings", None)
    if callable(getter):
        try:
            settings = getter(None, guild_id)
        except Exception:
            settings = None
    role_id = str(getattr(settings, "auto_role_id", "") or "").strip()
    if role_id:
        return role_id
    collection = getattr(repo, "guild_settings", None)
    if collection is None:
        return ""
    try:
        document = collection.find_one({"_id": guild_id})
    except Exception:
        return ""
    if not document:
        return ""
    return str(document.get("autoRoleId") or document.get("auto_role_id") or "").strip()


async def _resolve_bot_member(client: discord.Client, guild: discord.Guild) -> discord.Member | None:
    me = getattr(guild, "me", None)
    if isinstance(me, discord.Member):
        return me
    user = getattr(client, "user", None)
    if user is None:
        return None
    cached = guild.get_member(int(user.id))
    if cached is not None:
        return cached
    try:
        return await guild.fetch_member(int(user.id))
    except Exception:
        return None


async def _resolve_role(guild: discord.Guild, role_id: str) -> discord.Role | None:
    try:
        snowflake = int(role_id)
    except ValueError:
        return None
    role = guild.get_role(snowflake)
    if role is not None:
        return role
    try:
        roles = await guild.fetch_roles()
    except Exception:
        return None
    for candidate in roles:
        if candidate.id == snowflake:
            return candidate
    return None


def _autorole_is_safe(role: discord.Role, bot_member: discord.Member) -> bool:
    if role.is_default():
        return False
    if getattr(role, "managed", False):
        return False
    if role.permissions.administrator:
        return False
    if role.position >= bot_member.top_role.position:
        return False
    return role.is_assignable()


def _auto_unmute_user_ids_for_guild(repo: Repository, guild_id: str) -> list[str]:
    getter = getattr(repo, "get_auto_unmute_user_ids", None)
    if callable(getter):
        try:
            return getter(None, guild_id)
        except Exception:
            return []
    settings = None
    settings_getter = getattr(repo, "get_guild_settings", None)
    if callable(settings_getter):
        try:
            settings = settings_getter(None, guild_id)
        except Exception:
            return []
    return list(getattr(settings, "auto_unmute_user_ids", []) or [])


async def main() -> None:
    configure_logging("gateway")
    cfg = load_config()
    if cfg.discord_token == "":
        raise SystemExit("DISCORD_TOKEN is required")
    require_event_signing_secret(cfg.event_signing_secret)
    logger.info("gateway service starting guild=%s", cfg.discord_guild_id)

    mongo_client = MongoClient(cfg.mongo_uri)
    repo = Repository(mongo_client[cfg.mongo_db])

    nats = NATS()
    await nats.connect(cfg.nats_url)
    bus = Bus(nats, cfg.event_signing_secret, "gateway")

    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    intents.members = True
    client = discord.Client(intents=intents)
    GatewayService(client, bus).install()

    @client.event
    async def on_member_join(member: discord.Member) -> None:
        if str(getattr(member.guild, "id", "") or "") != cfg.discord_guild_id:
            return
        if getattr(member, "bot", False):
            return
        role_id = _autorole_id_for_guild(repo, str(member.guild.id))
        if role_id == "":
            return
        role = await _resolve_role(member.guild, role_id)
        if role is None:
            logger.warning("autorole skipped guild=%s missing role=%s", member.guild.id, role_id)
            return
        bot_member = await _resolve_bot_member(client, member.guild)
        if bot_member is None:
            logger.warning("autorole skipped guild=%s missing bot member", member.guild.id)
            return
        if not _autorole_is_safe(role, bot_member):
            logger.warning("autorole skipped guild=%s unsafe role=%s", member.guild.id, role_id)
            return
        try:
            await member.add_roles(role, reason="Voice Tracker autorole")
        except Exception:
            logger.exception("autorole assignment failed guild=%s member=%s role=%s", member.guild.id, member.id, role_id)

    async def _on_voice_state_update_unmute(
        member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if str(getattr(member.guild, "id", "") or "") != cfg.discord_guild_id:
            return
        if getattr(member, "bot", False):
            return
        if before.mute is True or after.mute is not True:
            return
        user_id = str(member.id)
        auto_unmute_ids = _auto_unmute_user_ids_for_guild(repo, str(member.guild.id))
        if user_id not in auto_unmute_ids:
            return
        bot_member = await _resolve_bot_member(client, member.guild)
        if bot_member is None:
            logger.warning("auto-unmute skipped guild=%s missing bot member", member.guild.id)
            return
        if not bot_member.guild_permissions.mute_members:
            logger.warning("auto-unmute skipped guild=%s missing mute_members permission", member.guild.id)
            return
        try:
            await member.edit(mute=False, reason="Voice Tracker auto-unmute")
            logger.info("auto-unmute applied guild=%s user=%s", member.guild.id, user_id)
        except discord.Forbidden:
            logger.warning("auto-unmute forbidden guild=%s user=%s", member.guild.id, user_id)
        except Exception:
            logger.exception("auto-unmute failed guild=%s user=%s", member.guild.id, user_id)

    client.add_listener(_on_voice_state_update_unmute, "on_voice_state_update")

    async def handle_summary(payload: bytes) -> None:
        event = summary_from_payload(payload)
        if event.channel_id == "" or event.message == "":
            return
        session = repo.get_session_by_id(None, event.session_id)
        if (
            session is None
            or session.guild_id != event.guild_id
            or session.summary_channel_id != event.channel_id
            or session.summary_message != event.message
        ):
            return
        claimed = repo.claim_session_summary_delivery(None, event.session_id, datetime.now(UTC))
        if not claimed:
            return
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await _send_summary(client, event.channel_id, event.message)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep((attempt + 1) * 0.25)
                continue
            repo.mark_session_summary_delivered(None, event.session_id, datetime.now(UTC))
            return
        repo.release_session_summary_delivery_claim(None, event.session_id)
        if last_error is not None:
            logger.exception(
                "summary delivery failed after retries session_id=%s guild_id=%s channel_id=%s",
                event.session_id,
                event.guild_id,
                event.channel_id,
                exc_info=last_error,
            )
            raise last_error

    await bus.subscribe(None, domain.SUBJECT_SUMMARY_READY, repo, handle_summary)

    await client.login(cfg.discord_token)
    await _deliver_pending(client, repo)

    async def sweep_pending() -> None:
        while True:
            await asyncio.sleep(60)
            await _deliver_pending(client, repo)

    sweep = asyncio.create_task(sweep_pending())
    try:
        await client.connect()
    finally:
        sweep.cancel()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
