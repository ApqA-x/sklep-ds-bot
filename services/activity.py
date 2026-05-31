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


def _append_fact(lines: list[str], label: str, value: str) -> None:
    clean = _snippet(value, limit=120)
    if clean:
        lines.append(f"**{label}:** {clean}")


def _embed_description(event: domain.ActivityEvent) -> str:
    return str(_template_payload(event).get("description", ""))


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
        _append_content_block(lines, "Before", _metadata_text(event, "before_content"))
        _append_content_block(lines, "After", _metadata_text(event, "after_content"))
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
        "footer": "Voice Tracker Activity",
    }


def _template_payload(event: domain.ActivityEvent) -> dict[str, object]:
    if event.event_type == domain.ACTIVITY_EVENT_MEMBER_JOIN:
        return activity_member_join.render(member_label=_member_label(event))
    if event.event_type == domain.ACTIVITY_EVENT_MEMBER_LEAVE:
        return activity_member_leave.render(member_label=_member_label(event))
    if event.event_type == domain.ACTIVITY_EVENT_INVITE_CREATE:
        return activity_invite_create.render(
            invite_code=event.invite_code,
            invite_url=event.invite_url,
            actor_label=_actor_label(event),
        )
    if event.event_type == domain.ACTIVITY_EVENT_INVITE_DELETE:
        return activity_invite_delete.render(
            invite_code=event.invite_code,
            invite_url=event.invite_url,
            actor_label=_actor_label(event),
        )
    if event.event_type == domain.ACTIVITY_EVENT_INVITE_USED:
        return activity_invite_used.render(
            member_label=_member_label(event),
            attribution_status=event.attribution_status,
            invite_code=event.invite_code,
            invite_url=event.invite_url,
            actor_label=_actor_label(event),
            exact_status_value=domain.INVITE_ATTRIBUTION_STATUS_EXACT,
        )
    if event.event_type in {
        domain.ACTIVITY_EVENT_MESSAGE_CREATE,
        domain.ACTIVITY_EVENT_MESSAGE_UPDATE,
        domain.ACTIVITY_EVENT_MESSAGE_DELETE,
        domain.ACTIVITY_EVENT_REACTION_ADD,
        domain.ACTIVITY_EVENT_REACTION_REMOVE,
    }:
        return _message_activity_payload(event)
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
