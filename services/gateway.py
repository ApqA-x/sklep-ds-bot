from __future__ import annotations

import asyncio
import logging
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
INVITE_ATTRIBUTION_SOURCE_LIVE_DIFF = "live_diff"
INVITE_CATALOG_SOURCE_GATEWAY_EVENT = "live_event"
INVITE_CATALOG_SOURCE_RECONCILIATION = "audit_log"
INVITE_CATALOG_SOURCE_SNAPSHOT = "snapshot"
INVITE_TYPE_REGULAR = "regular"
INVITE_TYPE_VANITY = "vanity"
ATTRIBUTION_STATUS_EXACT = "exact"
ATTRIBUTION_STATUS_AMBIGUOUS = "ambiguous"
ATTRIBUTION_STATUS_UNKNOWN = "unknown"
UNKNOWN_REASON_CONCURRENT_CANDIDATES = "concurrent_candidates"
UNKNOWN_REASON_DUPLICATE_JOIN_EVENT = "duplicate_join_event"
UNKNOWN_REASON_GATEWAY_DOWNTIME = "gateway_downtime"
UNKNOWN_REASON_MISSING_PERMISSIONS = "missing_permissions"
UNKNOWN_REASON_NO_USAGE_DELTA = "no_usage_delta"
UNKNOWN_REASON_SEED_UNAVAILABLE = "seed_unavailable"
UNKNOWN_REASON_SNAPSHOT_FETCH_FAILED = "snapshot_fetch_failed"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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


def _invite_code(invite: object | None) -> str:
    return str(getattr(invite, "code", "") or "").strip()


def _invite_uses(invite: object | None) -> int:
    uses = getattr(invite, "uses", 0)
    try:
        value = int(uses or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _invite_url(invite: object | None, code: str) -> str:
    url = str(getattr(invite, "url", "") or "").strip()
    if url != "":
        return url
    if code == "":
        return ""
    return f"https://discord.gg/{code}"


def _invite_inviter_user_id(invite: object | None) -> str:
    direct = str(getattr(invite, "inviter_user_id", "") or getattr(invite, "inviterUserId", "") or "").strip()
    if direct != "":
        return direct
    inviter = getattr(invite, "inviter", None)
    return str(getattr(inviter, "id", "") or "").strip()


def _invite_inviter_name(invite: object | None) -> str:
    direct = str(getattr(invite, "inviter_name", "") or getattr(invite, "inviterName", "") or "").strip()
    if direct != "":
        return direct
    inviter = getattr(invite, "inviter", None)
    if inviter is None:
        return ""
    for attr in ("display_name", "global_name", "name"):
        value = str(getattr(inviter, attr, "") or "").strip()
        if value != "":
            return value
    return ""


def _invite_target_channel_id(invite: object | None) -> str:
    return _channel_id(
        getattr(invite, "channel", None)
        or getattr(invite, "channel_id", None)
        or getattr(invite, "channelId", None)
    )


def _domain_object(type_name: str, **kwargs: object) -> object:
    cls = getattr(domain, type_name, None)
    if callable(cls):
        return cls(**kwargs)
    return SimpleNamespace(**kwargs)


def _snapshot_captured_at(snapshot: object | None) -> datetime | None:
    return _ensure_utc(getattr(snapshot, "captured_at", None) or getattr(snapshot, "capturedAt", None))


def _snapshot_invites(snapshot: object | None) -> list[object]:
    invites = getattr(snapshot, "invites", None)
    if isinstance(invites, list):
        return list(invites)
    return []


def _snapshot_by_code(snapshot: object | None) -> dict[str, object]:
    items: dict[str, object] = {}
    for invite in _snapshot_invites(snapshot):
        code = _invite_code(invite)
        if code == "":
            continue
        items[code] = invite
    return items


def _join_occurred_at(member: discord.Member) -> datetime:
    joined_at = _ensure_utc(getattr(member, "joined_at", None))
    return joined_at or _utc_now()


def _audit_target_code(entry: object | None) -> str:
    target = getattr(entry, "target", None)
    code = _invite_code(target)
    if code != "":
        return code
    for attr in ("after", "before", "extra"):
        nested = getattr(entry, attr, None)
        code = _invite_code(nested)
        if code != "":
            return code
    return ""


def _member_display_name(member: object | None) -> str:
    for attr in ("display_name", "global_name", "name"):
        value = str(getattr(member, attr, "") or "").strip()
        if value != "":
            return value
    return ""


def _activity_event(
    *,
    event_type: str,
    guild_id: str,
    occurred_at: datetime | None = None,
    member_user_id: str = "",
    member_name: str = "",
    actor_user_id: str = "",
    actor_name: str = "",
    invite_code: str = "",
    invite_url: str = "",
    attribution_status: str = "",
    metadata: dict[str, object] | None = None,
) -> domain.ActivityEvent:
    return domain.ActivityEvent(
        event_type=event_type,
        guild_id=guild_id,
        occurred_at=occurred_at or _utc_now(),
        member_user_id=member_user_id,
        member_name=member_name,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        invite_code=invite_code,
        invite_url=invite_url,
        attribution_status=attribution_status,
        metadata=dict(metadata or {}),
    )


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


def _member_role_ids(member: discord.Member) -> list[str]:
    role_ids: list[str] = []
    for role in list(getattr(member, "roles", []) or []):
        role_id = str(getattr(role, "id", "") or "").strip()
        if role_id == "":
            continue
        is_default = getattr(role, "is_default", None)
        if callable(is_default) and bool(is_default()):
            continue
        role_ids.append(role_id)
    return _normalize_ids(role_ids)


def _member_nickname(member: object | None) -> str:
    return str(getattr(member, "nick", "") or "").strip()


def _save_member_role_snapshot(repo: Repository, member: discord.Member) -> None:
    saver = getattr(repo, "save_member_role_snapshot", None)
    if not callable(saver):
        return
    guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
    user_id = str(getattr(member, "id", "") or "")
    if guild_id == "" or user_id == "":
        return
    saver(None, guild_id, user_id, _member_role_ids(member), _utc_now(), pending_restore=True)


def _sync_member_role_state(repo: Repository, member: discord.Member) -> None:
    saver = getattr(repo, "save_member_role_snapshot", None)
    if not callable(saver):
        return
    guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
    user_id = str(getattr(member, "id", "") or "")
    if guild_id == "" or user_id == "" or bool(getattr(member, "bot", False)):
        return
    saver(None, guild_id, user_id, _member_role_ids(member), _utc_now(), pending_restore=False)


def _sync_member_nickname_state(repo: Repository, member: discord.Member, *, source: str = "sync") -> None:
    guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
    user_id = str(getattr(member, "id", "") or "")
    if guild_id == "" or user_id == "" or bool(getattr(member, "bot", False)):
        return
    recorder = getattr(repo, "record_member_nickname", None)
    if callable(recorder):
        recorder(None, guild_id, user_id, _member_nickname(member), _utc_now(), source=source)
        return
    saver = getattr(repo, "save_member_nickname_snapshot", None)
    if callable(saver):
        saver(None, guild_id, user_id, _member_nickname(member), _utc_now())


def _record_member_nickname_change(
    repo: Repository,
    before: discord.Member,
    after: discord.Member,
    *,
    source: str,
) -> None:
    guild_id = str(getattr(getattr(after, "guild", None), "id", "") or getattr(getattr(before, "guild", None), "id", "") or "")
    user_id = str(getattr(after, "id", "") or getattr(before, "id", "") or "")
    if guild_id == "" or user_id == "" or bool(getattr(after, "bot", False)):
        return
    before_nickname = _member_nickname(before)
    after_nickname = _member_nickname(after)
    if before_nickname == after_nickname:
        return
    recorder = getattr(repo, "record_member_nickname", None)
    if callable(recorder):
        recorder(None, guild_id, user_id, after_nickname, _utc_now(), source=source, previous_nickname=before_nickname)
        return
    appender = getattr(repo, "append_member_nickname_change", None)
    saver = getattr(repo, "save_member_nickname_snapshot", None)
    now = _utc_now()
    if callable(appender):
        appender(
            None,
            domain.MemberNicknameChange(
                guild_id=guild_id,
                user_id=user_id,
                previous_nickname=before_nickname,
                nickname=after_nickname,
                changed_at=now,
                source=source,
            ),
        )
    if callable(saver):
        saver(None, guild_id, user_id, after_nickname, now)


async def _role_lookup_map(guild: discord.Guild) -> tuple[dict[str, discord.Role], bool]:
    mapping: dict[str, discord.Role] = {}
    for role in list(getattr(guild, "roles", []) or []):
        role_id = str(getattr(role, "id", "") or "")
        if role_id:
            mapping[role_id] = role
    try:
        for role in await guild.fetch_roles():
            role_id = str(getattr(role, "id", "") or "")
            if role_id:
                mapping[role_id] = role
        return mapping, True
    except Exception:
        return mapping, False


async def _restore_member_roles(
    client: discord.Client,
    repo: Repository,
    member: discord.Member,
    *,
    source: str,
) -> bool:
    guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
    user_id = str(getattr(member, "id", "") or "")
    if guild_id == "" or user_id == "":
        return True
    getter = getattr(repo, "get_member_role_state", None)
    if not callable(getter):
        return True
    state = getter(None, guild_id, user_id)
    if state is None:
        return True
    pending_restore = bool(getattr(state, "pending_restore", False))
    if source == "reconciliation" and not pending_restore:
        return True
    stored_ids = _normalize_ids(getattr(state, "role_ids", []) or getattr(state, "roles", []) or [])
    if len(stored_ids) == 0:
        marker = getattr(repo, "mark_member_roles_restored", None)
        if callable(marker):
            marker(None, guild_id, user_id, [], _utc_now())
        return True
    bot_member = await _resolve_bot_member(client, member.guild)
    if bot_member is None:
        logger.warning("role restore skipped guild=%s member=%s source=%s missing bot member", guild_id, user_id, source)
        return False

    roles_by_id, fetch_ok = await _role_lookup_map(member.guild)
    current_ids = set(_member_role_ids(member))
    retained_ids: set[str] = set(current_ids)
    transient_unresolved = False
    assignment_failed = False

    for role_id in stored_ids:
        role = roles_by_id.get(role_id)
        if role is None:
            if fetch_ok:
                logger.info(
                    "role restore pruned missing role guild=%s member=%s role=%s source=%s",
                    guild_id,
                    user_id,
                    role_id,
                    source,
                )
            else:
                retained_ids.add(role_id)
                transient_unresolved = True
            continue
        if role_id in current_ids:
            retained_ids.add(role_id)
            continue
        if not _autorole_is_safe(role, bot_member):
            logger.warning("role restore skipped unsafe role guild=%s member=%s role=%s source=%s", guild_id, user_id, role_id, source)
            continue
        try:
            await member.add_roles(role, reason="Voice Tracker role restore")
            retained_ids.add(role_id)
        except Exception:
            retained_ids.add(role_id)
            assignment_failed = True
            logger.exception(
                "role restore assignment failed guild=%s member=%s role=%s source=%s",
                guild_id,
                user_id,
                role_id,
                source,
            )

    marker = getattr(repo, "mark_member_roles_restored", None)
    saver = getattr(repo, "save_member_role_snapshot", None)
    if transient_unresolved or assignment_failed:
        if callable(saver):
            try:
                saver(
                    None,
                    guild_id,
                    user_id,
                    sorted(retained_ids),
                    getattr(state, "last_seen_at", None) or _utc_now(),
                    pending_restore=True,
                )
            except Exception:
                logger.exception(
                    "role restore transient state update failed guild=%s member=%s source=%s",
                    guild_id,
                    user_id,
                    source,
                )
        else:
            logger.warning(
                "role restore deferred without persistence guild=%s member=%s source=%s",
                guild_id,
                user_id,
                source,
            )
        return False
    if callable(marker):
        try:
            marker(None, guild_id, user_id, sorted(retained_ids), _utc_now())
        except Exception:
            logger.exception("role restore state update failed guild=%s member=%s source=%s", guild_id, user_id, source)
            return False
    return True


async def _reconcile_member_roles(client: discord.Client, repo: Repository, guild_id: str, limit: int = 0) -> None:
    guild = _guild_from_client(client, guild_id)
    if guild is None:
        return
    loader = getattr(repo, "list_member_role_states_by_guild", None)
    if not callable(loader):
        return
    states = loader(None, guild_id, limit)
    mark_pending = getattr(repo, "mark_member_roles_pending", None)
    for state in states:
        user_id = str(getattr(state, "user_id", "") or "")
        if user_id == "":
            continue
        try:
            member = await _resolve_member(guild, user_id)
        except Exception:
            logger.exception("member role reconciliation resolve failed guild=%s member=%s", guild_id, user_id)
            continue
        if member is None:
            if callable(mark_pending) and not bool(getattr(state, "pending_restore", False)):
                try:
                    mark_pending(None, guild_id, user_id)
                except Exception:
                    logger.exception("member role reconciliation pending mark failed guild=%s member=%s", guild_id, user_id)
            continue
        if bool(getattr(member, "bot", False)):
            continue
        try:
            restored = await _restore_member_roles(client, repo, member, source="reconciliation")
        except Exception:
            logger.exception("member role reconciliation restore failed guild=%s member=%s", guild_id, user_id)
            continue
        if not restored:
            continue
        try:
            _sync_member_role_state(repo, member)
        except Exception:
            logger.exception("member role reconciliation sync failed guild=%s member=%s", guild_id, user_id)


async def _sync_current_guild_member_roles(client: discord.Client, repo: Repository, guild_id: str) -> None:
    guild = _guild_from_client(client, guild_id)
    if guild is None:
        return
    state_getter = getattr(repo, "get_member_role_state", None)
    for member in list(getattr(guild, "members", []) or []):
        if bool(getattr(member, "bot", False)):
            continue
        if callable(state_getter):
            try:
                state = state_getter(None, guild_id, str(getattr(member, "id", "") or ""))
            except Exception:
                state = None
            if bool(getattr(state, "pending_restore", False)):
                continue
        try:
            _sync_member_role_state(repo, member)
        except Exception:
            member_id = str(getattr(member, "id", "") or "")
            logger.exception("member role sync failed guild=%s member=%s", guild_id, member_id)


async def _sync_current_guild_member_nicknames(client: discord.Client, repo: Repository, guild_id: str) -> None:
    guild = _guild_from_client(client, guild_id)
    if guild is None:
        return
    for member in list(getattr(guild, "members", []) or []):
        if bool(getattr(member, "bot", False)):
            continue
        try:
            _sync_member_nickname_state(repo, member, source="guild_sync")
        except Exception:
            member_id = str(getattr(member, "id", "") or "")
            logger.exception("member nickname sync failed guild=%s member=%s", guild_id, member_id)


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


def _voice_effect_sound_id(effect: object) -> str:
    sound = getattr(effect, "sound", None)
    if sound is not None:
        sound_id = str(getattr(sound, "id", "") or "")
        if sound_id:
            return sound_id
    sound_id = str(getattr(effect, "sound_id", "") or getattr(effect, "soundboard_sound_id", "") or "")
    if sound_id:
        return sound_id
    soundboard_sound = getattr(effect, "soundboard_sound", None)
    if soundboard_sound is not None:
        return str(getattr(soundboard_sound, "id", "") or "")
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
class InviteAttributionController:
    client: discord.Client
    repo: Repository
    guild_id: str
    snapshot_refresh_seconds: int = 60
    reconciliation_max_age_days: int = 45
    snapshot_sync_default: bool = True
    live_attribution_default: bool = True
    reconciliation_default: bool = False
    on_attribution: Any | None = None
    _join_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _ready: dict[str, bool] = field(default_factory=dict)

    async def seed_on_ready(self) -> None:
        snapshot_sync_enabled, _, _ = self._effective_feature_flags()
        if not snapshot_sync_enabled:
            self._ready[self.guild_id] = False
            return
        guild = _guild_from_client(self.client, self.guild_id)
        if guild is None:
            self._ready[self.guild_id] = False
            return
        self._ready[self.guild_id] = await self._seed_guild(guild)

    async def refresh_snapshot(self) -> None:
        snapshot_sync_enabled, _, _ = self._effective_feature_flags()
        if not snapshot_sync_enabled:
            return
        guild = _guild_from_client(self.client, self.guild_id)
        if guild is None:
            return
        await self._seed_guild(guild)

    async def on_invite_create(self, invite: object) -> None:
        snapshot_sync_enabled, _, _ = self._effective_feature_flags()
        if not snapshot_sync_enabled:
            return
        guild_id = _guild_id(invite)
        if guild_id != self.guild_id:
            return
        captured_at = _utc_now()
        entry = self._catalog_entry_from_invite(
            guild_id,
            invite,
            invite_type=INVITE_TYPE_REGULAR,
            observed_at=captured_at,
            source=INVITE_CATALOG_SOURCE_GATEWAY_EVENT,
        )
        self._upsert_catalog_entry(entry)
        await self.refresh_snapshot()

    async def on_invite_delete(self, invite: object) -> None:
        snapshot_sync_enabled, _, _ = self._effective_feature_flags()
        if not snapshot_sync_enabled:
            return
        guild_id = _guild_id(invite)
        if guild_id != self.guild_id:
            return
        code = _invite_code(invite)
        if code != "":
            marker = getattr(self.repo, "mark_invite_catalog_deleted", None) or getattr(self.repo, "mark_invite_deleted", None)
            if callable(marker):
                marker(None, guild_id, code, _utc_now(), INVITE_CATALOG_SOURCE_GATEWAY_EVENT)
        await self.refresh_snapshot()

    async def on_member_join(self, member: discord.Member) -> None:
        _, live_attribution_enabled, _ = self._effective_feature_flags()
        if not live_attribution_enabled:
            return
        guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
        if guild_id != self.guild_id or getattr(member, "bot", False):
            return
        lock = self._join_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            await self._attribute_join_locked(member)

    async def reconcile_metadata(self) -> None:
        _, _, reconciliation_enabled = self._effective_feature_flags()
        if not reconciliation_enabled:
            return
        guild = _guild_from_client(self.client, self.guild_id)
        if guild is None:
            return
        cutoff = _utc_now() - timedelta(days=max(1, self.reconciliation_max_age_days))
        audit_logs = getattr(guild, "audit_logs", None)
        if not callable(audit_logs):
            return
        actions = [
            getattr(discord.AuditLogAction, "invite_create", None),
            getattr(discord.AuditLogAction, "invite_update", None),
            getattr(discord.AuditLogAction, "invite_delete", None),
        ]
        for action in actions:
            if action is None:
                continue
            try:
                iterator = audit_logs(limit=100, action=action)
            except Exception:
                logger.exception("invite reconciliation failed guild=%s action=%s", self.guild_id, action)
                continue
            try:
                async for entry in iterator:
                    created_at = _ensure_utc(getattr(entry, "created_at", None))
                    if created_at is None or created_at < cutoff:
                        continue
                    code = _audit_target_code(entry)
                    if code == "":
                        continue
                    if action == getattr(discord.AuditLogAction, "invite_delete", object()):
                        marker = getattr(self.repo, "mark_invite_catalog_deleted", None) or getattr(self.repo, "mark_invite_deleted", None)
                        if callable(marker):
                            marker(None, self.guild_id, code, created_at, INVITE_CATALOG_SOURCE_RECONCILIATION)
                        continue
                    inviter = getattr(entry, "user", None)
                    entry_obj = _domain_object(
                        "InviteCatalogEntry",
                        guild_id=self.guild_id,
                        code=code,
                        url=_invite_url(getattr(entry, "target", None), code),
                        channel_id=_invite_target_channel_id(getattr(entry, "target", None)),
                        invite_type=INVITE_TYPE_REGULAR,
                        created_by_user_id=str(getattr(inviter, "id", "") or "").strip(),
                        created_by_name=_invite_inviter_name(SimpleNamespace(inviter=inviter)),
                        created_at=created_at,
                        last_seen_at=created_at,
                        source=INVITE_CATALOG_SOURCE_RECONCILIATION,
                    )
                    self._upsert_catalog_entry(entry_obj)
            except Exception:
                logger.exception("invite reconciliation iteration failed guild=%s action=%s", self.guild_id, action)

    async def _seed_guild(self, guild: discord.Guild) -> bool:
        snapshot = await self._fetch_current_snapshot(guild)
        if snapshot is None:
            self._ready[self.guild_id] = False
            return False
        self._upsert_snapshot(snapshot)
        self._sync_catalog_from_snapshot(snapshot)
        self._ready[self.guild_id] = True
        return True

    async def _attribute_join_locked(self, member: discord.Member) -> None:
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        guild_id = str(guild.id)
        joined_at = _join_occurred_at(member)
        previous_snapshot = self._get_snapshot(guild_id)
        if not self._ready.get(guild_id, False) or previous_snapshot is None:
            await self._persist_unknown(member, joined_at, UNKNOWN_REASON_SEED_UNAVAILABLE, previous_snapshot)
            return

        current_snapshot = await self._fetch_current_snapshot(guild)
        if current_snapshot is None:
            await self._persist_unknown(member, joined_at, UNKNOWN_REASON_SNAPSHOT_FETCH_FAILED, previous_snapshot)
            return

        candidates = self._diff_candidates(previous_snapshot, current_snapshot)
        attribution = self._build_attribution(member, joined_at, current_snapshot, candidates)
        created = self._write_attribution(attribution)
        if created:
            self._project_current_state(attribution)
            await self._publish_attribution(attribution)
        self._sync_catalog_from_snapshot(current_snapshot)
        self._upsert_snapshot(current_snapshot)

    async def _persist_unknown(
        self,
        member: discord.Member,
        joined_at: datetime,
        reason: str,
        snapshot: object | None,
    ) -> None:
        attribution = self._unknown_attribution(member, joined_at, reason, snapshot)
        created = self._write_attribution(attribution)
        if created:
            self._project_current_state(attribution)
            await self._publish_attribution(attribution)

    async def _fetch_current_snapshot(self, guild: discord.Guild) -> object | None:
        captured_at = _utc_now()
        invites_fetch = getattr(guild, "invites", None)
        if not callable(invites_fetch):
            return None
        try:
            invites = list(await invites_fetch())
        except discord.Forbidden:
            logger.warning("invite snapshot fetch forbidden guild=%s", guild.id)
            return None
        except Exception:
            logger.exception("invite snapshot fetch failed guild=%s", guild.id)
            return None

        snapshot_invites = [
            self._snapshot_entry_from_invite(
                guild_id=str(guild.id),
                invite=invite,
                invite_type=INVITE_TYPE_REGULAR,
            )
            for invite in invites
            if _invite_code(invite) != ""
        ]

        vanity_fetch = getattr(guild, "vanity_invite", None)
        if callable(vanity_fetch):
            try:
                vanity_invite = await vanity_fetch()
            except Exception:
                vanity_invite = None
            vanity_code = _invite_code(vanity_invite)
            if vanity_code != "":
                snapshot_invites.append(
                    self._snapshot_entry_from_invite(
                        guild_id=str(guild.id),
                        invite=vanity_invite,
                        invite_type=INVITE_TYPE_VANITY,
                    )
                )

        return _domain_object(
            "GuildInviteSnapshot",
            guild_id=str(guild.id),
            captured_at=captured_at,
            invites=snapshot_invites,
        )

    def _snapshot_entry_from_invite(self, guild_id: str, invite: object, invite_type: str) -> object:
        code = _invite_code(invite)
        return _domain_object(
            "InviteSnapshotEntry",
            code=code,
            uses=_invite_uses(invite),
            url=_invite_url(invite, code),
            channel_id=_invite_target_channel_id(invite),
            inviter_user_id=_invite_inviter_user_id(invite),
            inviter_name=_invite_inviter_name(invite),
            invite_type=invite_type,
        )

    def _catalog_entry_from_invite(self, guild_id: str, invite: object, *, invite_type: str, observed_at: datetime, source: str) -> object:
        code = _invite_code(invite)
        inviter_user_id = _invite_inviter_user_id(invite)
        inviter_name = _invite_inviter_name(invite)
        return _domain_object(
            "InviteCatalogEntry",
            guild_id=guild_id,
            code=code,
            url=_invite_url(invite, code),
            channel_id=_invite_target_channel_id(invite),
            invite_type=invite_type,
            created_by_user_id=inviter_user_id,
            created_by_name=inviter_name,
            last_seen_at=observed_at,
            source=source,
        )

    def _unknown_attribution(self, member: discord.Member, joined_at: datetime, reason: str, snapshot: object | None) -> object:
        guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
        user_id = str(getattr(member, "id", "") or "")
        return _domain_object(
            "MemberJoinAttribution",
            id=f"{guild_id}:{user_id}:{joined_at.isoformat()}",
            guild_id=guild_id,
            user_id=user_id,
            joined_at=joined_at,
            invite_code="",
            invite_url="",
            invite_type="",
            inviter_user_id="",
            inviter_name="",
            attribution_status=ATTRIBUTION_STATUS_UNKNOWN,
            candidate_codes=[],
            source=INVITE_ATTRIBUTION_SOURCE_LIVE_DIFF,
            snapshot_captured_at=_snapshot_captured_at(snapshot),
            internal_reason=reason,
            created_at=_utc_now(),
        )

    def _diff_candidates(self, previous_snapshot: object, current_snapshot: object) -> list[object]:
        previous = _snapshot_by_code(previous_snapshot)
        current = _snapshot_by_code(current_snapshot)
        candidates: list[object] = []
        for code, invite in current.items():
            previous_uses = _invite_uses(previous.get(code))
            current_uses = _invite_uses(invite)
            if current_uses > previous_uses:
                candidates.append(invite)
        return sorted(candidates, key=lambda item: _invite_code(item))

    def _build_attribution(
        self,
        member: discord.Member,
        joined_at: datetime,
        snapshot: object,
        candidates: list[object],
    ) -> object:
        guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
        user_id = str(getattr(member, "id", "") or "")
        candidate_codes = [_invite_code(candidate) for candidate in candidates if _invite_code(candidate) != ""]
        if len(candidate_codes) == 1:
            invite = candidates[0]
            return _domain_object(
                "MemberJoinAttribution",
                id=f"{guild_id}:{user_id}:{joined_at.isoformat()}",
                guild_id=guild_id,
                user_id=user_id,
                joined_at=joined_at,
                invite_code=_invite_code(invite),
                invite_url=_invite_url(invite, _invite_code(invite)),
                invite_type=str(getattr(invite, "invite_type", "") or getattr(invite, "inviteType", "") or ""),
                inviter_user_id=_invite_inviter_user_id(invite),
                inviter_name=_invite_inviter_name(invite),
                attribution_status=ATTRIBUTION_STATUS_EXACT,
                candidate_codes=candidate_codes,
                source=INVITE_ATTRIBUTION_SOURCE_LIVE_DIFF,
                snapshot_captured_at=_snapshot_captured_at(snapshot),
                internal_reason="",
                created_at=_utc_now(),
            )
        if len(candidate_codes) > 1:
            return _domain_object(
                "MemberJoinAttribution",
                id=f"{guild_id}:{user_id}:{joined_at.isoformat()}",
                guild_id=guild_id,
                user_id=user_id,
                joined_at=joined_at,
                invite_code="",
                invite_url="",
                invite_type="",
                inviter_user_id="",
                inviter_name="",
                attribution_status=ATTRIBUTION_STATUS_AMBIGUOUS,
                candidate_codes=candidate_codes,
                source=INVITE_ATTRIBUTION_SOURCE_LIVE_DIFF,
                snapshot_captured_at=_snapshot_captured_at(snapshot),
                internal_reason=UNKNOWN_REASON_CONCURRENT_CANDIDATES,
                created_at=_utc_now(),
            )
        return self._unknown_attribution(member, joined_at, UNKNOWN_REASON_NO_USAGE_DELTA, snapshot)

    def _get_snapshot(self, guild_id: str) -> object | None:
        getter = getattr(self.repo, "get_guild_invite_snapshot", None)
        if not callable(getter):
            return None
        return getter(None, guild_id)

    def _upsert_snapshot(self, snapshot: object) -> None:
        writer = getattr(self.repo, "upsert_guild_invite_snapshot", None)
        if callable(writer):
            writer(None, snapshot)

    def _upsert_catalog_entry(self, entry: object) -> None:
        writer = getattr(self.repo, "upsert_invite_catalog_entry", None)
        if callable(writer):
            writer(None, entry)

    def _sync_catalog_from_snapshot(self, snapshot: object) -> None:
        syncer = getattr(self.repo, "sync_invite_catalog_from_snapshot", None)
        if callable(syncer):
            syncer(None, snapshot, INVITE_CATALOG_SOURCE_SNAPSHOT)
            return
        guild_id = str(getattr(snapshot, "guild_id", "") or getattr(snapshot, "guildId", "") or "")
        observed_at = _snapshot_captured_at(snapshot) or _utc_now()
        for invite in _snapshot_invites(snapshot):
            entry = self._catalog_entry_from_invite(
                guild_id,
                invite,
                invite_type=str(getattr(invite, "invite_type", "") or getattr(invite, "inviteType", "") or INVITE_TYPE_REGULAR),
                observed_at=observed_at,
                source=INVITE_CATALOG_SOURCE_SNAPSHOT,
            )
            self._upsert_catalog_entry(entry)

    def _write_attribution(self, attribution: object) -> bool:
        writer = getattr(self.repo, "append_member_join_attribution", None) or getattr(
            self.repo, "create_member_join_attribution", None
        )
        if not callable(writer):
            return False
        try:
            return bool(writer(None, attribution))
        except Exception:
            logger.exception(
                "member join attribution write failed guild=%s user=%s",
                getattr(attribution, "guild_id", ""),
                getattr(attribution, "user_id", ""),
            )
            return False

    def _project_current_state(self, attribution: object) -> None:
        projector = getattr(self.repo, "project_member_join_state", None)
        if callable(projector):
            projector(None, attribution)

    async def _publish_attribution(self, attribution: object) -> None:
        callback = self.on_attribution
        if callback is None:
            return
        result = callback(attribution)
        if asyncio.iscoroutine(result):
            await result

    def _effective_feature_flags(self) -> tuple[bool, bool, bool]:
        settings = self.repo.get_guild_settings(None, self.guild_id)
        snapshot_enabled = bool(getattr(settings, "invite_snapshot_sync_enabled", self.snapshot_sync_default))
        live_enabled = bool(getattr(settings, "invite_live_attribution_enabled", self.live_attribution_default))
        reconcile_enabled = bool(getattr(settings, "invite_reconciliation_enabled", self.reconciliation_default))
        if live_enabled and not snapshot_enabled:
            snapshot_enabled = True
        return snapshot_enabled, live_enabled, reconcile_enabled


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
        effect_channel_id = _channel_id(getattr(effect, "channel", None) or getattr(effect, "channel_id", None))
        effect_user_id = _voice_effect_user_id(effect)
        effect_sound_id = _voice_effect_sound_id(effect)
        logger.info(
            "soundboard effect received guild=%s channel=%s user=%s sound=%s",
            guild_id or "-",
            effect_channel_id or "-",
            effect_user_id or "-",
            effect_sound_id or "-",
        )
        if guild_id != self.guild_id:
            logger.info(
                "soundboard enforcement skipped reason=guild_mismatch event_guild=%s expected_guild=%s",
                guild_id or "-",
                self.guild_id,
            )
            return
        if not _is_soundboard_effect(effect):
            logger.info("soundboard enforcement skipped reason=not_soundboard_effect guild=%s", guild_id)
            return
        settings = self.repo.get_guild_settings(None, guild_id)
        managed_channel_id = _managed_voice_channel_id(settings)
        enforcement_enabled = _soundboard_enforcement_enabled(settings)
        if managed_channel_id == "" or not enforcement_enabled:
            logger.info(
                "soundboard enforcement skipped reason=disabled_or_unmanaged guild=%s managed_channel=%s enabled=%s",
                guild_id,
                managed_channel_id or "-",
                enforcement_enabled,
            )
            return
        if effect_channel_id != managed_channel_id:
            logger.info(
                "soundboard enforcement skipped reason=channel_mismatch guild=%s effect_channel=%s managed_channel=%s",
                guild_id,
                effect_channel_id or "-",
                managed_channel_id,
            )
            return
        connected = await self.voice_controller.is_connected_to_managed_channel(guild_id, managed_channel_id)
        if not connected:
            logger.info(
                "soundboard enforcement skipped reason=bot_not_in_managed_channel guild=%s managed_channel=%s",
                guild_id,
                managed_channel_id,
            )
            return

        user_id = effect_user_id
        if user_id == "":
            logger.info("soundboard enforcement skipped reason=missing_sender guild=%s", guild_id)
            return
        guild = getattr(effect, "guild", None)
        if guild is None:
            guild = _guild_from_client(self.client, guild_id)
        if guild is None:
            logger.warning("soundboard enforcement skipped reason=guild_unavailable guild=%s user=%s", guild_id, user_id)
            return
        member = await _resolve_member(guild, user_id)
        if member is None:
            logger.info("soundboard enforcement skipped reason=member_not_found guild=%s user=%s", guild_id, user_id)
            return
        if bool(getattr(member, "bot", False)):
            logger.info("soundboard enforcement skipped reason=member_is_bot guild=%s user=%s", guild_id, user_id)
            return
        member_channel_id = _channel_id(getattr(getattr(member, "voice", None), "channel", None))
        if member_channel_id != managed_channel_id:
            logger.info(
                "soundboard enforcement skipped reason=member_not_in_managed_channel guild=%s user=%s member_channel=%s managed_channel=%s",
                guild_id,
                user_id,
                member_channel_id or "-",
                managed_channel_id,
            )
            return

        bot_member = await _resolve_bot_member(self.client, guild)
        managed_channel = await _resolve_managed_voice_channel(guild, managed_channel_id)
        if bot_member is None or managed_channel is None:
            logger.warning(
                "soundboard enforcement skipped reason=bot_or_channel_unavailable guild=%s user=%s bot=%s channel=%s",
                guild_id,
                user_id,
                bot_member is not None,
                managed_channel is not None,
            )
            return
        permissions = managed_channel.permissions_for(bot_member)
        if not bool(getattr(permissions, "move_members", False)):
            logger.warning("soundboard enforcement blocked guild=%s missing move_members", guild_id)
            return
        try:
            logger.info(
                "soundboard enforcement action=disconnect guild=%s user=%s channel=%s sound=%s",
                guild_id,
                user_id,
                managed_channel_id,
                effect_sound_id or "-",
            )
            await member.move_to(None, reason="Voice Tracker soundboard enforcement")
            logger.info("soundboard enforcement applied guild=%s user=%s", guild_id, user_id)
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
    repo.ensure_indexes(None)

    nats = NATS()
    await nats.connect(cfg.nats_url)
    bus = Bus(nats, cfg.event_signing_secret, "gateway")
    settings = repo.get_guild_settings(None, cfg.discord_guild_id) or domain.GuildSettings(guild_id=cfg.discord_guild_id)
    logger.info(
        "invite feature defaults guild=%s snapshot=%s live=%s reconciliation=%s userinfo=%s",
        cfg.discord_guild_id,
        bool(getattr(settings, "invite_snapshot_sync_enabled", True)),
        bool(getattr(settings, "invite_live_attribution_enabled", True)),
        bool(getattr(settings, "invite_reconciliation_enabled", False)),
        bool(getattr(settings, "invite_userinfo_enabled", True)),
    )

    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    intents.members = True
    client = discord.Client(intents=intents)
    GatewayService(client, bus).install()
    invite_attribution = InviteAttributionController(
        client=client,
        repo=repo,
        guild_id=cfg.discord_guild_id,
        snapshot_sync_default=bool(getattr(settings, "invite_snapshot_sync_enabled", True)),
        live_attribution_default=bool(getattr(settings, "invite_live_attribution_enabled", True)),
        reconciliation_default=bool(getattr(settings, "invite_reconciliation_enabled", False)),
    )
    voice_controller = ManagedVoiceController(client=client, repo=repo, guild_id=cfg.discord_guild_id)
    soundboard_enforcement = SoundboardEnforcement(
        client=client,
        repo=repo,
        guild_id=cfg.discord_guild_id,
        voice_controller=voice_controller,
    )

    async def publish_activity_event(event: domain.ActivityEvent) -> None:
        if event.guild_id != cfg.discord_guild_id:
            return
        await bus.publish_json(None, domain.SUBJECT_ACTIVITY_EVENT, event.to_dict())

    async def publish_invite_used_activity(attribution: object) -> None:
        event = _activity_event(
            event_type=domain.ACTIVITY_EVENT_INVITE_USED,
            guild_id=str(getattr(attribution, "guild_id", "") or ""),
            occurred_at=_ensure_utc(getattr(attribution, "joined_at", None)) or _utc_now(),
            member_user_id=str(getattr(attribution, "user_id", "") or ""),
            invite_code=str(getattr(attribution, "invite_code", "") or ""),
            invite_url=str(getattr(attribution, "invite_url", "") or ""),
            attribution_status=str(getattr(attribution, "attribution_status", "") or ""),
            actor_user_id=str(getattr(attribution, "inviter_user_id", "") or ""),
            actor_name=str(getattr(attribution, "inviter_name", "") or ""),
            metadata={"source": str(getattr(attribution, "source", "") or "")},
        )
        await publish_activity_event(event)

    invite_attribution.on_attribution = publish_invite_used_activity

    @client.event
    async def on_ready() -> None:
        await invite_attribution.seed_on_ready()
        await voice_controller.reconcile()
        await _reconcile_member_roles(client, repo, cfg.discord_guild_id)
        await _sync_current_guild_member_roles(client, repo, cfg.discord_guild_id)
        await _sync_current_guild_member_nicknames(client, repo, cfg.discord_guild_id)

    @client.event
    async def on_member_join(member: discord.Member) -> None:
        if str(getattr(member.guild, "id", "") or "") != cfg.discord_guild_id:
            return
        if getattr(member, "bot", False):
            return
        try:
            await publish_activity_event(
                _activity_event(
                    event_type=domain.ACTIVITY_EVENT_MEMBER_JOIN,
                    guild_id=str(member.guild.id),
                    occurred_at=_join_occurred_at(member),
                    member_user_id=str(member.id),
                    member_name=_member_display_name(member),
                )
            )
        except Exception:
            logger.exception("member join activity publish failed guild=%s member=%s", member.guild.id, member.id)
        restored = True
        try:
            await invite_attribution.on_member_join(member)
        except Exception:
            logger.exception("invite attribution failed guild=%s member=%s", member.guild.id, member.id)
        try:
            restored = await _restore_member_roles(client, repo, member, source="member_join")
        except Exception:
            restored = False
            logger.exception("role restore failed guild=%s member=%s", member.guild.id, member.id)
        if restored:
            try:
                _sync_member_role_state(repo, member)
            except Exception:
                logger.exception("role state sync failed guild=%s member=%s", member.guild.id, member.id)
        try:
            _sync_member_nickname_state(repo, member, source="member_join")
        except Exception:
            logger.exception("member nickname sync failed guild=%s member=%s", member.guild.id, member.id)
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

    @client.event
    async def on_member_remove(member: discord.Member) -> None:
        if str(getattr(member.guild, "id", "") or "") != cfg.discord_guild_id:
            return
        if getattr(member, "bot", False):
            return
        try:
            await publish_activity_event(
                _activity_event(
                    event_type=domain.ACTIVITY_EVENT_MEMBER_LEAVE,
                    guild_id=str(member.guild.id),
                    occurred_at=_utc_now(),
                    member_user_id=str(member.id),
                    member_name=_member_display_name(member),
                )
            )
        except Exception:
            logger.exception("member leave activity publish failed guild=%s member=%s", member.guild.id, member.id)
        try:
            _save_member_role_snapshot(repo, member)
        except Exception:
            logger.exception("role snapshot failed guild=%s member=%s", member.guild.id, member.id)
        try:
            _sync_member_nickname_state(repo, member, source="member_remove")
        except Exception:
            logger.exception("member nickname snapshot failed guild=%s member=%s", member.guild.id, member.id)

    @client.event
    async def on_member_update(before: discord.Member, after: discord.Member) -> None:
        if str(getattr(getattr(after, "guild", None), "id", "") or "") != cfg.discord_guild_id:
            return
        if getattr(after, "bot", False):
            return
        try:
            _record_member_nickname_change(repo, before, after, source="member_update")
        except Exception:
            logger.exception("member nickname update failed guild=%s member=%s", after.guild.id, after.id)

    @client.event
    async def on_invite_create(invite: object) -> None:
        try:
            await publish_activity_event(
                _activity_event(
                    event_type=domain.ACTIVITY_EVENT_INVITE_CREATE,
                    guild_id=_guild_id(invite),
                    occurred_at=_utc_now(),
                    actor_user_id=_invite_inviter_user_id(invite),
                    actor_name=_invite_inviter_name(invite),
                    invite_code=_invite_code(invite),
                    invite_url=_invite_url(invite, _invite_code(invite)),
                )
            )
        except Exception:
            logger.exception("invite create activity publish failed guild=%s code=%s", _guild_id(invite), _invite_code(invite))
        try:
            await invite_attribution.on_invite_create(invite)
        except Exception:
            logger.exception("invite create refresh failed guild=%s code=%s", _guild_id(invite), _invite_code(invite))

    @client.event
    async def on_invite_delete(invite: object) -> None:
        try:
            await publish_activity_event(
                _activity_event(
                    event_type=domain.ACTIVITY_EVENT_INVITE_DELETE,
                    guild_id=_guild_id(invite),
                    occurred_at=_utc_now(),
                    actor_user_id=_invite_inviter_user_id(invite),
                    actor_name=_invite_inviter_name(invite),
                    invite_code=_invite_code(invite),
                    invite_url=_invite_url(invite, _invite_code(invite)),
                )
            )
        except Exception:
            logger.exception("invite delete activity publish failed guild=%s code=%s", _guild_id(invite), _invite_code(invite))
        try:
            await invite_attribution.on_invite_delete(invite)
        except Exception:
            logger.exception("invite delete refresh failed guild=%s code=%s", _guild_id(invite), _invite_code(invite))

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
            try:
                await _deliver_pending(client, repo)
            except Exception:
                logger.exception("pending summary sweep failed")

    async def reconcile_managed_voice() -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await voice_controller.reconcile()
            except Exception:
                logger.exception("managed voice reconciliation iteration failed guild=%s", cfg.discord_guild_id)

    async def refresh_invite_snapshots() -> None:
        while True:
            await asyncio.sleep(max(5, invite_attribution.snapshot_refresh_seconds))
            try:
                await invite_attribution.refresh_snapshot()
            except Exception:
                logger.exception("invite snapshot refresh failed guild=%s", cfg.discord_guild_id)

    async def reconcile_invite_metadata() -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await invite_attribution.reconcile_metadata()
            except Exception:
                logger.exception("invite metadata reconciliation iteration failed guild=%s", cfg.discord_guild_id)

    async def reconcile_member_roles() -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await _reconcile_member_roles(client, repo, cfg.discord_guild_id)
                await _sync_current_guild_member_roles(client, repo, cfg.discord_guild_id)
                await _sync_current_guild_member_nicknames(client, repo, cfg.discord_guild_id)
            except Exception:
                logger.exception("member role reconciliation iteration failed guild=%s", cfg.discord_guild_id)

    sweep = asyncio.create_task(sweep_pending())
    reconcile = asyncio.create_task(reconcile_managed_voice())
    invite_refresh = asyncio.create_task(refresh_invite_snapshots())
    invite_reconcile = asyncio.create_task(reconcile_invite_metadata())
    role_reconcile = asyncio.create_task(reconcile_member_roles())
    try:
        await client.connect()
    finally:
        sweep.cancel()
        reconcile.cancel()
        invite_refresh.cancel()
        invite_reconcile.cancel()
        role_reconcile.cancel()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
