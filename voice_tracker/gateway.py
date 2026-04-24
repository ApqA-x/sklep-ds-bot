from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import Any, Protocol

from .domain import SUBJECT_VOICE_EVENT, SummaryReadyEvent, VoiceStateEvent


class Publisher(Protocol):
    def publish_json(self, ctx: Any, subject: str, value: Any) -> Any:
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _channel_id(channel: Any) -> str:
    if channel is None:
        return ""
    if isinstance(channel, str):
        return channel.strip()
    return str(getattr(channel, "id", "") or getattr(channel, "channel_id", "") or "")


def _guild_id(source: Any) -> str:
    if source is None:
        return ""
    guild = getattr(source, "guild", None)
    if guild is not None and getattr(guild, "id", ""):
        return str(getattr(guild, "id"))
    channel = getattr(source, "channel", None)
    if channel is not None:
        guild = getattr(channel, "guild", None)
        if guild is not None and getattr(guild, "id", ""):
            return str(getattr(guild, "id"))
    return str(getattr(source, "guild_id", "") or "")


def _member_name(member: Any) -> str:
    if member is None:
        return ""
    for attr in ("display_name", "global_name", "name", "username"):
        value = getattr(member, attr, "")
        if value:
            return str(value)
    user = getattr(member, "user", None)
    if user is not None:
        for attr in ("display_name", "global_name", "name", "username"):
            value = getattr(user, attr, "")
            if value:
                return str(value)
    return ""


def _member_is_bot(member: Any) -> bool:
    if member is None:
        return False
    user = getattr(member, "user", member)
    return bool(getattr(user, "bot", False))


def _lookup_member(session: Any, guild_id: str, user_id: str) -> Any:
    if session is None:
        return None
    guild = None
    get_guild = getattr(session, "get_guild", None)
    if callable(get_guild):
        for candidate in (guild_id, int(guild_id) if str(guild_id).isdigit() else guild_id):
            guild = get_guild(candidate)
            if guild is not None:
                break
    elif hasattr(session, "guilds"):
        for item in getattr(session, "guilds", []):
            if str(getattr(item, "id", "")) == str(guild_id):
                guild = item
                break
    if guild is None:
        return None
    get_member = getattr(guild, "get_member", None)
    if callable(get_member):
        try:
            return get_member(int(user_id))
        except Exception:
            return get_member(user_id)
    members = getattr(guild, "members", None)
    if members is None:
        return None
    for member in members:
        member_id = str(getattr(member, "id", getattr(getattr(member, "user", None), "id", "")))
        if member_id == str(user_id):
            return member
    return None


def voice_event_from_discord(session: Any, update: Any) -> VoiceStateEvent:
    if update is None:
        return VoiceStateEvent()

    before = getattr(update, "before", None) or getattr(update, "before_update", None)
    after = getattr(update, "after", None) or update
    member_from_update = getattr(update, "member", None)
    guild_id = str(
        getattr(update, "guild_id", "")
        or _guild_id(member_from_update)
        or _guild_id(after)
        or _guild_id(before)
    )
    user_id = str(getattr(update, "user_id", "") or getattr(after, "user_id", "") or "")

    previous_channel_id = _channel_id(getattr(before, "channel", None) or getattr(before, "channel_id", None))
    channel_id = _channel_id(getattr(after, "channel", None) or getattr(after, "channel_id", None))

    member = member_from_update or _lookup_member(session, guild_id, user_id)
    return VoiceStateEvent(
        guild_id=guild_id,
        user_id=user_id,
        user_name=_member_name(member),
        channel_id=channel_id,
        previous_channel_id=previous_channel_id,
        is_bot=_member_is_bot(member),
        occurred_at=getattr(update, "occurred_at", None) or getattr(after, "occurred_at", None) or _utc_now(),
    )


def install_event_listener(session: Any, event_name: str, listener: Any) -> bool:
    if session is None:
        return False

    add_listener = getattr(session, "add_listener", None)
    if callable(add_listener):
        add_listener(listener, event_name)
        return True

    add_handler = getattr(session, "add_handler", None)
    if callable(add_handler):
        add_handler(listener)
        return True

    existing = getattr(session, event_name, None)

    async def _composed(*args: Any, **kwargs: Any) -> None:
        if callable(existing):
            result = existing(*args, **kwargs)
            if inspect.isawaitable(result):
                await result
        result = listener(*args, **kwargs)
        if inspect.isawaitable(result):
            await result

    setattr(session, event_name, _composed)
    return True


class Service:
    def __init__(self, session: Any, bus: Publisher) -> None:
        self.session = session
        self.bus = bus

    def install(self) -> None:
        if self.session is None:
            return

        async def _listener(member: Any, before: Any, after: Any) -> None:
            update = type(
                "_VoiceUpdate",
                (),
                {
                    "guild_id": _guild_id(member) or _guild_id(after) or _guild_id(before),
                    "user_id": str(getattr(member, "id", "")),
                    "member": member,
                    "before_update": before,
                    "after": after,
                },
            )()
            event = voice_event_from_discord(self.session, update)
            result = self.bus.publish_json(None, SUBJECT_VOICE_EVENT, event)
            if inspect.isawaitable(result):
                await result

        install_event_listener(self.session, "on_voice_state_update", _listener)


def summary_from_payload(payload: bytes | bytearray | str) -> SummaryReadyEvent:
    try:
        if isinstance(payload, (bytes, bytearray)):
            raw = bytes(payload).decode("utf-8")
        else:
            raw = str(payload)
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"decode summary event: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("decode summary event: invalid payload")
    return SummaryReadyEvent.from_dict(data)
