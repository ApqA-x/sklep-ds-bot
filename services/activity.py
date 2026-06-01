from __future__ import annotations

import asyncio
import difflib
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

from services.chat_templates import activity_invite_create
from services.chat_templates import activity_invite_delete
from services.chat_templates import activity_invite_used
from services.chat_templates import activity_member_join
from services.chat_templates import activity_member_leave
from services.chat_templates import activity_unknown_event
from voice_tracker import domain
from voice_tracker.bus import Bus
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret
from voice_tracker.timeutil import discord_timestamp, go_duration, parse_datetime, positive_delta


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _event_enabled(repo: Repository, guild_id: str, event_type: str) -> bool:
    settings = repo.get_guild_settings(None, guild_id)
    if settings is None:
        settings = domain.GuildSettings(guild_id=guild_id)
    enabled = set(domain.clean_activity_event_types(getattr(settings, "activity_event_types", [])))
    return event_type in enabled


def _activity_channel_id(repo: Repository, guild_id: str, event_type: str = "") -> str:
    settings = repo.get_guild_settings(None, guild_id)
    if settings is None:
        return ""
    category = domain.activity_event_category(event_type)
    category_channels = domain.clean_activity_category_channel_ids(getattr(settings, "activity_category_channel_ids", {}))
    if category and category_channels.get(category, ""):
        return category_channels[category]
    return str(getattr(settings, "activity_channel_id", "") or "").strip()


def _member_label(event: domain.ActivityEvent) -> str:
    return _label_with_mention(event.member_name, event.member_user_id)


def _actor_label(event: domain.ActivityEvent) -> str:
    return _label_with_mention(event.actor_name, event.actor_user_id)


def _label_with_mention(name: str, user_id: str) -> str:
    clean_name = str(name or "").strip()
    clean_user_id = str(user_id or "").strip()
    mention = f"<@{clean_user_id}>" if clean_user_id else ""
    if clean_name and mention:
        return f"{clean_name} {mention}"
    if clean_name:
        return clean_name
    if mention:
        return mention
    return "unknown"


def _metadata_text(event: domain.ActivityEvent, key: str) -> str:
    return str(event.metadata.get(key, "") or "").strip()


def _channel_label(event: domain.ActivityEvent) -> str:
    channel_id = _metadata_text(event, "channel_id")
    return f"<#{channel_id}>" if channel_id else "unknown channel"


def _message_link(event: domain.ActivityEvent) -> str:
    channel_id = _metadata_text(event, "channel_id")
    message_id = _metadata_text(event, "message_id")
    if event.guild_id and channel_id and message_id:
        return f"https://discord.com/channels/{event.guild_id}/{channel_id}/{message_id}"
    return ""


def _metadata_url(event: domain.ActivityEvent, key: str) -> str:
    value = _metadata_text(event, key)
    if value.startswith(("http://", "https://")):
        return value
    return ""


def _metadata_datetime(event: domain.ActivityEvent, key: str) -> datetime | None:
    try:
        return parse_datetime(event.metadata.get(key))
    except (TypeError, ValueError):
        return None


def _server_age_text(joined_at: datetime | None, left_at: datetime | None) -> str:
    if joined_at is None or left_at is None:
        return ""
    return go_duration(positive_delta(left_at - joined_at), round_seconds=True)


def _snippet(value: str, *, limit: int = 500) -> str:
    clean = str(value or "").strip()
    clean = clean.replace("@everyone", "@ everyone").replace("@here", "@ here")
    clean = clean.replace("```", "`\u200b``")
    if clean == "":
        return ""
    if len(clean) > limit:
        clean = f"{clean[: limit - 1]}..."
    return clean


def _append_content_block(lines: list[str], label: str, value: str) -> None:
    snippet = _snippet(value)
    if snippet:
        lines.append(f"**{label}:**\n```text\n{snippet}\n```")
    else:
        lines.append(f"**{label}:** unavailable")


def _diff_lines(before: str, after: str, *, limit: int = 900) -> str:
    before_lines = (_snippet(before, limit=limit) or "").splitlines() or [""]
    after_lines = (_snippet(after, limit=limit) or "").splitlines() or [""]
    diff = list(difflib.ndiff(before_lines, after_lines))
    rendered: list[str] = []
    for line in diff:
        if line.startswith("- "):
            rendered.append(f"- {line[2:]}")
        elif line.startswith("+ "):
            rendered.append(f"+ {line[2:]}")
        elif line.startswith("  "):
            rendered.append(f"  {line[2:]}")
    output = "\n".join(rendered).strip("\n")
    if not output:
        output = "- unavailable\n+ unavailable"
    if len(output) > limit:
        output = f"{output[: limit - 4]}\n..."
    return output


def _append_diff_block(lines: list[str], before: str, after: str) -> None:
    lines.append(f"**Changes:**\n```diff\n{_diff_lines(before, after)}\n```")


def _append_fact(lines: list[str], label: str, value: str) -> None:
    clean = _snippet(value, limit=120)
    if clean:
        lines.append(f"**{label}:** {clean}")


def _user_id_fact(lines: list[str], user_id: str) -> None:
    _append_fact(lines, "User ID", user_id)


def _voice_activity_payload(event: domain.ActivityEvent) -> dict[str, object]:
    member = _member_label(event)
    from_channel = _metadata_text(event, "previous_channel_id")
    to_channel = _metadata_text(event, "channel_id")
    if event.event_type == domain.ACTIVITY_EVENT_VOICE_JOIN:
        title = "Voice joined"
        lines = [f"**Member:** {member}", f"**Channel:** <#{to_channel}>" if to_channel else "**Channel:** unknown"]
    elif event.event_type == domain.ACTIVITY_EVENT_VOICE_LEAVE:
        title = "Voice left"
        lines = [f"**Member:** {member}", f"**Channel:** <#{from_channel}>" if from_channel else "**Channel:** unknown"]
    else:
        title = "Voice moved"
        lines = [
            f"**Member:** {member}",
            f"**From:** <#{from_channel}>" if from_channel else "**From:** unknown",
            f"**To:** <#{to_channel}>" if to_channel else "**To:** unknown",
        ]
        actor_id = str(event.actor_user_id or "").strip()
        actor = _actor_label(event) if actor_id else "unknown (audit log unavailable)"
        lines.append(f"**Moved by:** {actor}")
    _user_id_fact(lines, event.member_user_id)
    return {"title": title, "description": "\n".join(lines), "color": 0x57F287, "footer": "Voice Log"}


def _profile_activity_payload(event: domain.ActivityEvent) -> dict[str, object]:
    member = _member_label(event)
    actor_id = str(event.actor_user_id or "").strip()
    actor = _actor_label(event) if actor_id else "unknown (audit log unavailable)"
    lines = [f"**Member:** {member}", f"**Changed by:** {actor}"]
    _user_id_fact(lines, event.member_user_id)
    if event.event_type == domain.ACTIVITY_EVENT_PROFILE_NICKNAME_UPDATE:
        title = "Nickname changed"
        _append_fact(lines, "Before", _metadata_text(event, "before_nickname") or "none")
        _append_fact(lines, "After", _metadata_text(event, "after_nickname") or "none")
    elif event.event_type == domain.ACTIVITY_EVENT_PROFILE_ROLES_UPDATE:
        title = "Roles changed"
        added = _metadata_text(event, "added_roles")
        removed = _metadata_text(event, "removed_roles")
        if added:
            lines.append(f"**Added:** {added}")
        if removed:
            lines.append(f"**Removed:** {removed}")
        if not added and not removed:
            lines.append("**Roles:** changed")
    else:
        return activity_unknown_event.render(payload=event.to_dict())
    return {"title": title, "description": "\n".join(lines), "color": 0xFEE75C, "footer": "Profile Activity"}


def _embed_description(event: domain.ActivityEvent) -> str:
    return str(_template_payload(event).get("description", ""))


def _join_leave_activity_payload(event: domain.ActivityEvent) -> dict[str, object]:
    if event.event_type == domain.ACTIVITY_EVENT_MEMBER_JOIN:
        title = "Member joined"
        lines = [f"**Member:** {_member_label(event)}"]
        _user_id_fact(lines, event.member_user_id)
    elif event.event_type == domain.ACTIVITY_EVENT_MEMBER_LEAVE:
        leave_reason = _metadata_text(event, "leave_reason") or "leaved"
        title = f"Member {leave_reason}"
        lines = [f"**Member:** {_member_label(event)}"]
        lines.append(f"**Result:** {leave_reason}")
        if leave_reason in {"banned", "kicked"}:
            actor_id = str(event.actor_user_id or "").strip()
            actor = _actor_label(event) if actor_id else "unknown (audit log unavailable)"
            label = "Banned by" if leave_reason == "banned" else "Kicked by"
            lines.append(f"**{label}:** {actor}")
        joined_at = _metadata_datetime(event, "joined_at")
        if joined_at is not None:
            lines.append(f"**Joined:** {discord_timestamp(joined_at)}")
        server_age = _server_age_text(joined_at, event.occurred_at)
        if server_age:
            lines.append(f"**Time on server:** {server_age}")
        roles = _metadata_text(event, "roles")
        lines.append(f"**Roles:** {roles or 'none'}")
        _user_id_fact(lines, event.member_user_id)
    elif event.event_type == domain.ACTIVITY_EVENT_INVITE_USED:
        title = "Invite used"
        lines = [f"**Member:** {_member_label(event)}"]
        _user_id_fact(lines, event.member_user_id)
        if event.invite_url:
            lines.append(f"**Invite:** {event.invite_url}")
        _append_fact(lines, "Invite code", event.invite_code)
        if event.attribution_status == domain.INVITE_ATTRIBUTION_STATUS_EXACT:
            lines.append(f"**Inviter:** {_actor_label(event)}")
        else:
            _append_fact(lines, "Attribution", event.attribution_status or "unknown")
    elif event.event_type == domain.ACTIVITY_EVENT_INVITE_CREATE:
        title = "Invite created"
        lines = [f"**Created by:** {_actor_label(event)}"]
        if event.invite_url:
            lines.append(f"**Invite:** {event.invite_url}")
        _append_fact(lines, "Invite code", event.invite_code)
    elif event.event_type == domain.ACTIVITY_EVENT_INVITE_DELETE:
        title = "Invite deleted"
        lines = [f"**Deleted by:** {_actor_label(event)}"]
        if event.invite_url:
            lines.append(f"**Invite:** {event.invite_url}")
        _append_fact(lines, "Invite code", event.invite_code)
    else:
        return activity_unknown_event.render(payload=event.to_dict())
    return {"title": title, "description": "\n".join(lines), "color": 0x5865F2, "footer": "Join Leave Activity"}


def _message_activity_payload(event: domain.ActivityEvent) -> dict[str, object]:
    actor = _actor_label(event)
    member = _member_label(event)
    channel = _channel_label(event)
    link = _message_link(event)
    message_id = _metadata_text(event, "message_id")
    if event.event_type == domain.ACTIVITY_EVENT_MESSAGE_CREATE:
        title = "Message sent"
        lines = [f"**Author:** {member}", f"**Channel:** {channel}"]
        _append_fact(lines, "Message ID", message_id)
        _append_content_block(lines, "Content", _metadata_text(event, "content"))
    elif event.event_type == domain.ACTIVITY_EVENT_MESSAGE_UPDATE:
        title = "Message edited"
        lines = [f"**Author:** {member}", f"**Channel:** {channel}"]
        _append_fact(lines, "Message ID", message_id)
        _append_diff_block(lines, _metadata_text(event, "before_content"), _metadata_text(event, "after_content"))
    elif event.event_type == domain.ACTIVITY_EVENT_MESSAGE_DELETE:
        title = "Message deleted"
        lines = [f"**Author:** {member}", f"**Channel:** {channel}"]
        _append_fact(lines, "Message ID", message_id)
        _append_content_block(lines, "Deleted content", _metadata_text(event, "content"))
    elif event.event_type == domain.ACTIVITY_EVENT_REACTION_ADD:
        title = "Reaction added"
        lines = [
            f"**Actor:** {actor}",
            f"**Message author:** {member}",
            f"**Channel:** {channel}",
            f"**Reaction:** {_metadata_text(event, 'emoji') or 'unknown emoji'}",
        ]
        _append_fact(lines, "Message ID", message_id)
    elif event.event_type == domain.ACTIVITY_EVENT_REACTION_REMOVE:
        title = "Reaction removed"
        lines = [
            f"**Actor:** {actor}",
            f"**Message author:** {member}",
            f"**Channel:** {channel}",
            f"**Reaction:** {_metadata_text(event, 'emoji') or 'unknown emoji'}",
        ]
        _append_fact(lines, "Message ID", message_id)
    else:
        return activity_unknown_event.render(payload=event.to_dict())
    if link:
        lines.append(f"**Message:** {link}")
    return {
        "title": title,
        "description": "\n".join(lines),
        "color": 0x5865F2,
        "footer": "Message Activity",
    }


def _template_payload(event: domain.ActivityEvent) -> dict[str, object]:
    if event.event_type in {
        domain.ACTIVITY_EVENT_MEMBER_JOIN,
        domain.ACTIVITY_EVENT_MEMBER_LEAVE,
        domain.ACTIVITY_EVENT_INVITE_CREATE,
        domain.ACTIVITY_EVENT_INVITE_DELETE,
        domain.ACTIVITY_EVENT_INVITE_USED,
    }:
        return _join_leave_activity_payload(event)
    if event.event_type in {
        domain.ACTIVITY_EVENT_MESSAGE_CREATE,
        domain.ACTIVITY_EVENT_MESSAGE_UPDATE,
        domain.ACTIVITY_EVENT_MESSAGE_DELETE,
        domain.ACTIVITY_EVENT_REACTION_ADD,
        domain.ACTIVITY_EVENT_REACTION_REMOVE,
    }:
        return _message_activity_payload(event)
    if event.event_type in {
        domain.ACTIVITY_EVENT_VOICE_JOIN,
        domain.ACTIVITY_EVENT_VOICE_LEAVE,
        domain.ACTIVITY_EVENT_VOICE_MOVE,
    }:
        return _voice_activity_payload(event)
    if event.event_type in {
        domain.ACTIVITY_EVENT_PROFILE_NICKNAME_UPDATE,
        domain.ACTIVITY_EVENT_PROFILE_ROLES_UPDATE,
    }:
        return _profile_activity_payload(event)
    return activity_unknown_event.render(payload=event.to_dict())


def _build_embed(event: domain.ActivityEvent) -> discord.Embed:
    payload = _template_payload(event)
    embed = discord.Embed(
        title=str(payload.get("title", "Activity Event")),
        description=str(payload.get("description", "")),
        color=int(payload.get("color", 0x5865F2)),
        timestamp=event.occurred_at or _utc_now(),
    )
    embed.set_footer(text=str(payload.get("footer", "Voice Tracker Activity")))
    if event.event_type in {domain.ACTIVITY_EVENT_REACTION_ADD, domain.ACTIVITY_EVENT_REACTION_REMOVE}:
        avatar_url = _metadata_url(event, "actor_avatar_url") or _metadata_url(event, "member_avatar_url")
    else:
        avatar_url = _metadata_url(event, "member_avatar_url") or _metadata_url(event, "actor_avatar_url")
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def _activity_event_from_payload(payload: bytes) -> domain.ActivityEvent:
    body = json.loads(payload.decode("utf-8"))
    return domain.ActivityEvent.from_dict(body)


async def _resolve_channel(client: discord.Client, channel_id: str):
    snowflake = int(channel_id)
    channel = client.get_channel(snowflake)
    if channel is None:
        channel = await client.fetch_channel(snowflake)
    return channel


async def _send_activity(client: discord.Client, channel_id: str, embed: discord.Embed) -> None:
    channel = await _resolve_channel(client, channel_id)
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


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
            event = _activity_event_from_payload(payload)
        except Exception:
            logger.exception("invalid activity payload")
            return
        if event.guild_id != cfg.discord_guild_id:
            return
        if event.event_type not in domain.ACTIVITY_EVENT_TYPES:
            return
        channel_id = _activity_channel_id(repo, event.guild_id, event.event_type)
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
