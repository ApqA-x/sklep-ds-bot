"""Fail-closed evidence for a future wall-clock voice sleep timer.

Session participants are written asynchronously by another service.  They are
useful history, but cannot prove that someone was still in voice at a deadline.
This tracker only trusts observations made by the current connected gateway
generation and cross-checks its in-memory state with Mongo and the live cache.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Callable, Iterable
from uuid import uuid4


def _channel_id(state: Any) -> str:
    channel = getattr(state, "channel", None)
    return str(getattr(channel, "id", "") or "")


def _key(guild_id: str, user_id: str) -> str:
    return f"{guild_id}:{user_id}"


class VoicePresenceTracker:
    """Conservative, gateway-local evidence; never reconstruct a past stay.

    All operations for one gateway are serialized.  A queued transition marks
    its user dirty *before* awaiting the lock, so a later worker cannot mistake
    a persisted pre-leave observation for current evidence.  A failed Mongo
    write invalidates local evidence instead of trusting the previous document.
    """

    def __init__(self, collection: Any) -> None:
        self.collection = collection
        self._lock = asyncio.Lock()
        self._generation = uuid4().hex
        self._ready = False
        self._states: dict[str, dict[str, Any]] = {}
        self._dirty: dict[str, int] = {}

    def disconnected(self) -> None:
        # Called before any await in the Discord disconnect callback.
        self._ready = False
        self._generation = uuid4().hex
        self._states.clear()

    async def seed(self, guilds: Iterable[Any], allowed_guild_id: str = "") -> None:
        """A new READY/RESUMED epoch starts at observation time, never earlier."""
        self.disconnected()
        generation = self._generation
        async with self._lock:
            now = datetime.now(UTC)
            for guild in guilds:
                guild_id = str(getattr(guild, "id", "") or "")
                if not guild_id or (allowed_guild_id and guild_id != allowed_guild_id):
                    continue
                if getattr(guild, "unavailable", False):
                    continue
                states = getattr(guild, "voice_states", None)
                if states is None:
                    states = getattr(guild, "_voice_states", {})
                for user, state in states.items():
                    channel_id = _channel_id(state)
                    if channel_id:
                        user_id = str(getattr(user, "id", user))
                        await self._persist(guild_id, user_id, channel_id, now, now)
            # A disconnect callback can run while the snapshot is writing.
            # Such a snapshot must never resurrect a disconnected generation.
            if self._generation == generation:
                self._ready = True

    async def observe(self, member: Any, before: Any, after: Any) -> None:
        guild_id = str(getattr(getattr(member, "guild", None), "id", "") or "")
        user_id = str(getattr(member, "id", "") or "")
        if not guild_id or not user_id:
            return
        key = _key(guild_id, user_id)
        self._dirty[key] = self._dirty.get(key, 0) + 1
        try:
            async with self._lock:
                if not self._ready:
                    return
                before_id = _channel_id(before)
                after_id = _channel_id(after)
                if before_id == after_id:
                    return  # mute/deafen/stream updates cannot start a stay
                now = datetime.now(UTC)
                previous = self._states.get(key)
                # A channel move preserves a stay only when we observed its
                # preceding state in this same connected generation.
                continuous_since = (
                    previous["continuousSince"]
                    if before_id and after_id and previous is not None
                    and previous["channelId"] == before_id
                    else now if after_id else None
                )
                await self._persist(guild_id, user_id, after_id, continuous_since, now)
        finally:
            remaining = self._dirty[key] - 1
            if remaining:
                self._dirty[key] = remaining
            else:
                self._dirty.pop(key, None)

    async def _persist(
        self, guild_id: str, user_id: str, channel_id: str,
        continuous_since: datetime | None, observed_at: datetime,
    ) -> None:
        key = _key(guild_id, user_id)
        doc = {
            "_id": key,
            "guildId": guild_id,
            "userId": user_id,
            "channelId": channel_id,
            "present": bool(channel_id),
            "continuousSince": continuous_since,
            "lastObservedAt": observed_at,
            "gatewayGeneration": self._generation,
        }
        # Invalidate memory before I/O: a failed leave write must not retain
        # an old positive observation in this process.
        self._states.pop(key, None)
        await asyncio.to_thread(self.collection.replace_one, {"_id": key}, doc, upsert=True)
        self._states[key] = doc

    async def proves_presence(
        self, guild_id: str, user_id: str, due_at: datetime,
        live_channel_id: str, *, gateway_ready: bool,
        lease_check: Callable[[], None] | None = None,
    ) -> bool:
        """True only if the current uninterrupted stay began by ``due_at``.

        The caller must check the live Discord member immediately before the
        REST action as well.  A future worker must hold its execution guard
        through that final check and the one-shot action.
        """
        key = _key(guild_id, user_id)
        if not self._ready or not gateway_ready or not live_channel_id or self._dirty.get(key):
            return False
        if due_at.tzinfo is None or due_at > datetime.now(UTC):
            return False
        async with self._lock:
            if not self._ready or self._dirty.get(key):
                return False
            if lease_check is not None:
                await asyncio.to_thread(lease_check)
            local = self._states.get(key)
            if local is None or not local["present"] or local["channelId"] != live_channel_id:
                return False
            if local["gatewayGeneration"] != self._generation:
                return False
            since = local["continuousSince"]
            # Windows and some VMs expose a coarse wall clock.  Equality may
            # mean the join actually followed the deadline in the same tick.
            if since is None or since >= due_at:
                return False
            stored = await asyncio.to_thread(self.collection.find_one, {"_id": key})
            return bool(
                stored is not None
                and stored.get("gatewayGeneration") == self._generation
                and stored.get("present") is True
                and stored.get("channelId") == live_channel_id
                and stored.get("continuousSince") == since
                and stored.get("lastObservedAt") == local["lastObservedAt"]
            )
