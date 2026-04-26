from __future__ import annotations

import asyncio
import logging
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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
from voice_tracker.gateway import Service as GatewayService, install_event_listener, summary_from_payload
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret


SUMMARY_EMBED_COLOR = 0x5865F2
logger = logging.getLogger(__name__)


def _channel_id(channel: object | None) -> str:
    if channel is None:
        return ""
    if isinstance(channel, str):
        return channel.strip()
    return str(getattr(channel, "id", "") or getattr(channel, "channel_id", "") or "")


def _guild_id(source: object | None) -> str:
    if source is None:
        return ""
    guild = getattr(source, "guild", None)
    if guild is not None:
        guild_id = str(getattr(guild, "id", "") or "")
        if guild_id:
            return guild_id
    channel = getattr(source, "channel", None)
    if channel is not None:
        guild = getattr(channel, "guild", None)
        guild_id = str(getattr(guild, "id", "") or "")
        if guild_id:
            return guild_id
    return str(getattr(source, "guild_id", "") or "")


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


def _normalize_ids(values: object) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for raw in values if isinstance(values, (list, tuple, set)) else []:
        value = str(raw or "").strip()
        if value == "" or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    return sorted(ids)


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


async def _resolve_member(guild: discord.Guild, user_id: str) -> discord.Member | None:
    try:
        snowflake = int(user_id)
    except ValueError:
        return None
    cached = guild.get_member(snowflake)
    if cached is not None:
        return cached
    fetch_member = getattr(guild, "fetch_member", None)
    if not callable(fetch_member):
        return None
    try:
        return await fetch_member(snowflake)
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


def _voice_state_is_muted(state: object) -> bool:
    return bool(getattr(state, "mute", False))


def _voice_state_is_deafened(state: object) -> bool:
    return bool(getattr(state, "deaf", False))


def _auto_unmute_user_ids_for_guild(repo: Repository, guild_id: str) -> list[str]:
    getter = getattr(repo, "get_auto_unmute_user_ids", None)
    if callable(getter):
        try:
            return _normalize_ids(getter(None, guild_id))
        except Exception:
            return []
    settings = None
    settings_getter = getattr(repo, "get_guild_settings", None)
    if callable(settings_getter):
        try:
            settings = settings_getter(None, guild_id)
        except Exception:
            return []
    return _normalize_ids(getattr(settings, "auto_unmute_user_ids", []) or [])


def _managed_voice_channel_id(settings: domain.GuildSettings | None) -> str:
    if settings is None:
        return ""
    return str(getattr(settings, "managed_voice_channel_id", "") or "").strip()


def _soundboard_enforcement_enabled(settings: domain.GuildSettings | None) -> bool:
    return bool(getattr(settings, "soundboard_enforcement_enabled", False)) if settings is not None else False


def _guild_from_client(client: discord.Client, guild_id: str) -> discord.Guild | None:
    if guild_id == "":
        return None
    get_guild = getattr(client, "get_guild", None)
    if callable(get_guild):
        try:
            snowflake = int(guild_id)
        except ValueError:
            snowflake = None
        if snowflake is not None:
            guild = get_guild(snowflake)
            if guild is not None:
                return guild
        guild = get_guild(guild_id)
        if guild is not None:
            return guild
    for guild in list(getattr(client, "guilds", []) or []):
        if str(getattr(guild, "id", "") or "") == guild_id:
            return guild
    return None


def _guild_voice_client(client: discord.Client, guild: discord.Guild) -> discord.VoiceClient | None:
    voice_client = getattr(guild, "voice_client", None)
    if voice_client is not None:
        return voice_client
    for candidate in list(getattr(client, "voice_clients", []) or []):
        candidate_guild = getattr(candidate, "guild", None)
        if candidate_guild is not None and str(getattr(candidate_guild, "id", "") or "") == str(guild.id):
            return candidate
    return None


def _voice_client_connected(client: object | None) -> bool:
    if client is None:
        return False
    is_connected = getattr(client, "is_connected", None)
    if callable(is_connected):
        try:
            return bool(is_connected())
        except Exception:
            return False
    return getattr(client, "channel", None) is not None


async def _safe_voice_disconnect(client: object | None) -> None:
    if client is None:
        return
    disconnect = getattr(client, "disconnect", None)
    if not callable(disconnect):
        return
    try:
        await disconnect()
    except Exception:
        logger.exception("managed voice disconnect failed")


async def _resolve_managed_voice_channel(guild: discord.Guild, channel_id: str) -> discord.abc.GuildChannel | None:
    try:
        snowflake = int(channel_id)
    except ValueError:
        return None
    channel = guild.get_channel(snowflake)
    if channel is None:
        try:
            channel = await guild.fetch_channel(snowflake)
        except Exception:
            return None
    if channel is None:
        return None
    if getattr(channel, "type", None) not in {discord.ChannelType.voice, discord.ChannelType.stage_voice}:
        return None
    return channel


def _bot_connected_channel_id(bot_member: discord.Member | None) -> str:
    voice = getattr(bot_member, "voice", None)
    return _channel_id(getattr(voice, "channel", None))


def _is_soundboard_effect(effect: object) -> bool:
    for name in ("sound_id", "soundboard_sound_id", "sound", "soundboard_sound"):
        value = getattr(effect, name, None)
        if value is not None and str(value) != "":
            return True
    return False


def _voice_effect_user_id(effect: object) -> str:
    user_id = str(getattr(effect, "user_id", "") or "")
    if user_id:
        return user_id
    user = getattr(effect, "user", None)
    if user is not None:
        user_id = str(getattr(user, "id", "") or "")
        if user_id:
            return user_id
    return ""


def _set_managed_connected_at(repo: Repository, guild_id: str, connected_at: datetime | None) -> None:
    settings = repo.get_guild_settings(None, guild_id)
    if connected_at is None:
        if settings is None or settings.managed_voice_connected_at is None:
            return
        settings.managed_voice_connected_at = None
        repo.upsert_guild_settings(None, settings)
        return
    if settings is None:
        settings = domain.GuildSettings(guild_id=guild_id)
    if settings.managed_voice_connected_at is not None:
        return
    settings.managed_voice_connected_at = connected_at
    repo.upsert_guild_settings(None, settings)


@dataclass(slots=True)
class ManagedVoiceController:
    client: discord.Client
    repo: Repository
    guild_id: str
    reconnect_backoff_seconds: int = 5
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _retry_after: dict[str, datetime] = field(default_factory=dict)

    async def reconcile(self) -> None:
        await self.reconcile_guild(self.guild_id)

    async def reconcile_guild(self, guild_id: str) -> None:
        guild_id = str(guild_id or "").strip()
        if guild_id == "" or guild_id != self.guild_id:
            return
        now = datetime.now(UTC)
        retry_after = self._retry_after.get(guild_id)
        if retry_after is not None and now < retry_after:
            return
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            try:
                await self._reconcile_guild_locked(guild_id)
            except Exception:
                self._retry_after[guild_id] = datetime.now(UTC) + timedelta(seconds=max(1, self.reconnect_backoff_seconds))
                logger.exception("managed voice reconcile failed guild=%s", guild_id)
            else:
                self._retry_after.pop(guild_id, None)

    async def _reconcile_guild_locked(self, guild_id: str) -> None:
        settings = self.repo.get_guild_settings(None, guild_id)
        managed_channel_id = _managed_voice_channel_id(settings)
        guild = _guild_from_client(self.client, guild_id)
        if guild is None:
            _set_managed_connected_at(self.repo, guild_id, None)
            return

        voice_client = _guild_voice_client(self.client, guild)
        if managed_channel_id == "":
            if _voice_client_connected(voice_client):
                await _safe_voice_disconnect(voice_client)
            _set_managed_connected_at(self.repo, guild_id, None)
            return

        channel = await _resolve_managed_voice_channel(guild, managed_channel_id)
        if channel is None:
            logger.warning("managed voice channel missing guild=%s channel_id=%s", guild_id, managed_channel_id)
            _set_managed_connected_at(self.repo, guild_id, None)
            return

        bot_member = await _resolve_bot_member(self.client, guild)
        if bot_member is None:
            _set_managed_connected_at(self.repo, guild_id, None)
            return
        permissions = channel.permissions_for(bot_member)
        if not bool(getattr(permissions, "view_channel", False)) or not bool(getattr(permissions, "connect", False)):
            logger.warning(
                "managed voice connect blocked guild=%s channel_id=%s view=%s connect=%s",
                guild_id,
                managed_channel_id,
                bool(getattr(permissions, "view_channel", False)),
                bool(getattr(permissions, "connect", False)),
            )
            _set_managed_connected_at(self.repo, guild_id, None)
            return

        if voice_client is None:
            await channel.connect()
        elif not _voice_client_connected(voice_client):
            await _safe_voice_disconnect(voice_client)
            await channel.connect()
        else:
            current_channel_id = _channel_id(getattr(voice_client, "channel", None))
            if current_channel_id != managed_channel_id:
                await voice_client.move_to(channel)

        refreshed_bot_member = await _resolve_bot_member(self.client, guild)
        if _bot_connected_channel_id(refreshed_bot_member) == managed_channel_id:
            _set_managed_connected_at(self.repo, guild_id, datetime.now(UTC))
            return
        _set_managed_connected_at(self.repo, guild_id, None)

    async def is_connected_to_managed_channel(self, guild_id: str, managed_channel_id: str) -> bool:
        guild = _guild_from_client(self.client, str(guild_id or "").strip())
        if guild is None:
            return False
        bot_member = await _resolve_bot_member(self.client, guild)
        if bot_member is None:
            return False
        return _bot_connected_channel_id(bot_member) == str(managed_channel_id or "").strip()

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
        if guild_id != self.guild_id:
            return
        bot_user = getattr(self.client, "user", None)
        bot_user_id = str(getattr(bot_user, "id", "") or "")
        if str(getattr(member, "id", "") or "") != bot_user_id:
            return
        before_channel_id = _channel_id(getattr(before, "channel", None))
        after_channel_id = _channel_id(getattr(after, "channel", None))
        if before_channel_id == after_channel_id:
            return
        await self.reconcile_guild(guild_id)


@dataclass(slots=True)
class SoundboardEnforcement:
    client: discord.Client
    repo: Repository
    guild_id: str
    voice_controller: ManagedVoiceController

    async def on_voice_channel_effect(self, effect: object) -> None:
        guild_id = _guild_id(effect)
        if guild_id != self.guild_id:
            return
        if not _is_soundboard_effect(effect):
            return
        settings = self.repo.get_guild_settings(None, guild_id)
        managed_channel_id = _managed_voice_channel_id(settings)
        if managed_channel_id == "" or not _soundboard_enforcement_enabled(settings):
            return
        effect_channel_id = _channel_id(getattr(effect, "channel", None) or getattr(effect, "channel_id", None))
        if effect_channel_id != managed_channel_id:
            return
        if not await self.voice_controller.is_connected_to_managed_channel(guild_id, managed_channel_id):
            return

        user_id = _voice_effect_user_id(effect)
        if user_id == "":
            return
        guild = getattr(effect, "guild", None)
        if guild is None:
            guild = _guild_from_client(self.client, guild_id)
        if guild is None:
            return
        member = await _resolve_member(guild, user_id)
        if member is None or bool(getattr(member, "bot", False)):
            return
        member_channel_id = _channel_id(getattr(getattr(member, "voice", None), "channel", None))
        if member_channel_id != managed_channel_id:
            return

        bot_member = await _resolve_bot_member(self.client, guild)
        managed_channel = await _resolve_managed_voice_channel(guild, managed_channel_id)
        if bot_member is None or managed_channel is None:
            return
        permissions = managed_channel.permissions_for(bot_member)
        if not bool(getattr(permissions, "move_members", False)):
            logger.warning("soundboard enforcement blocked guild=%s missing move_members", guild_id)
            return
        try:
            await member.move_to(None, reason="Voice Tracker soundboard enforcement")
        except discord.Forbidden:
            logger.warning("soundboard enforcement forbidden guild=%s user=%s", guild_id, user_id)
        except Exception:
            logger.exception("soundboard enforcement failed guild=%s user=%s", guild_id, user_id)


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
    voice_controller = ManagedVoiceController(client=client, repo=repo, guild_id=cfg.discord_guild_id)
    soundboard_enforcement = SoundboardEnforcement(
        client=client,
        repo=repo,
        guild_id=cfg.discord_guild_id,
        voice_controller=voice_controller,
    )

    @client.event
    async def on_ready() -> None:
        await voice_controller.reconcile()

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
        current_state = getattr(member, "voice", None) or after
        should_clear_mute = _voice_state_is_muted(after) or _voice_state_is_muted(current_state)
        should_clear_deafen = _voice_state_is_deafened(after) or _voice_state_is_deafened(current_state)
        if not should_clear_mute and not should_clear_deafen:
            return
        user_id = str(member.id)
        auto_unmute_ids = _auto_unmute_user_ids_for_guild(repo, str(member.guild.id))
        if user_id not in auto_unmute_ids:
            return
        bot_member = await _resolve_bot_member(client, member.guild)
        if bot_member is None:
            logger.warning("auto-unmute skipped guild=%s missing bot member", member.guild.id)
            return
        permissions = getattr(bot_member, "guild_permissions", None)
        edit_kwargs: dict[str, bool] = {}
        missing_permissions: list[str] = []
        if should_clear_mute:
            if bool(getattr(permissions, "mute_members", False)):
                edit_kwargs["mute"] = False
            else:
                missing_permissions.append("mute_members")
        if should_clear_deafen:
            if bool(getattr(permissions, "deafen_members", False)):
                edit_kwargs["deafen"] = False
            else:
                missing_permissions.append("deafen_members")
        if not edit_kwargs:
            logger.warning(
                "auto-unmute skipped guild=%s missing permissions=%s",
                member.guild.id,
                ",".join(missing_permissions),
            )
            return
        if missing_permissions:
            logger.warning(
                "auto-unmute partial guild=%s user=%s missing permissions=%s",
                member.guild.id,
                user_id,
                ",".join(missing_permissions),
            )
        await asyncio.sleep(0.25)
        for attempt in range(3):
            try:
                await member.edit(reason="Voice Tracker auto-unmute", **edit_kwargs)
            except discord.Forbidden:
                logger.warning("auto-unmute forbidden guild=%s user=%s", member.guild.id, user_id)
                return
            except Exception:
                if attempt == 2:
                    logger.exception("auto-unmute failed guild=%s user=%s", member.guild.id, user_id)
                    return
            else:
                refreshed_member = await _resolve_member(member.guild, user_id)
                refreshed_state = getattr(refreshed_member, "voice", None) if refreshed_member is not None else None
                mute_cleared = "mute" not in edit_kwargs or refreshed_state is None or not _voice_state_is_muted(refreshed_state)
                deafen_cleared = (
                    "deafen" not in edit_kwargs
                    or refreshed_state is None
                    or not _voice_state_is_deafened(refreshed_state)
                )
                if mute_cleared and deafen_cleared:
                    logger.info(
                        "auto-unmute applied guild=%s user=%s attempt=%s",
                        member.guild.id,
                        user_id,
                        attempt + 1,
                    )
                    return
            await asyncio.sleep(0.25 * (attempt + 1))
        logger.warning(
            "auto-unmute did not clear states guild=%s user=%s states=%s",
            member.guild.id,
            user_id,
            ",".join(sorted(edit_kwargs)),
        )

    install_event_listener(client, "on_voice_state_update", _on_voice_state_update_unmute)
    install_event_listener(client, "on_voice_state_update", voice_controller.on_voice_state_update)
    install_event_listener(client, "on_voice_channel_effect", soundboard_enforcement.on_voice_channel_effect)

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

    async def reconcile_managed_voice() -> None:
        while True:
            await asyncio.sleep(5)
            await voice_controller.reconcile()

    sweep = asyncio.create_task(sweep_pending())
    reconcile = asyncio.create_task(reconcile_managed_voice())
    try:
        await client.connect()
    finally:
        sweep.cancel()
        reconcile.cancel()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
