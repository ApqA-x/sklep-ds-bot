"""One-shot gateway execution of due voice sleep timers.

This module deliberately makes no automatic Discord retry.  A timeout after
the PATCH may mean Discord applied it; a successor records ``unknown`` instead
of disconnecting a possibly new voice stay.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .sleep_timers import SleepTimerStore
from .supervise import SingleWriterLeaseLost
from .voice_presence import VoicePresenceTracker


logger = logging.getLogger(__name__)


def _live_channel(member: Any) -> Any | None:
    return getattr(getattr(member, "voice", None), "channel", None)


def _bot_can_move(guild: Any, channel: Any, client: Any) -> bool:
    bot_member = getattr(guild, "me", None)
    if bot_member is None and getattr(client, "user", None) is not None:
        bot_member = guild.get_member(client.user.id)
    if bot_member is None:
        return False
    permissions_for = getattr(channel, "permissions_for", None)
    if callable(permissions_for):
        try:
            return bool(permissions_for(bot_member).move_members)
        except Exception:
            return False
    return False


class SleepTimerExecutor:
    def __init__(
        self, *, store: SleepTimerStore, presence: VoicePresenceTracker,
        client: Any, lease: Any, allowed_guild_id: str = "",
        discord_token: str, settle_seconds: float = 0.5,
    ) -> None:
        self.store = store
        self.presence = presence
        self.client = client
        self.lease = lease
        self.allowed_guild_id = allowed_guild_id
        self.discord_token = discord_token
        self.settle_seconds = settle_seconds

    async def recover_stale(self) -> int:
        await asyncio.to_thread(self.lease.ensure_current)
        return await asyncio.to_thread(
            self.store.mark_stale_unknown, current_fence=self.lease.fence,
            current_owner=self.lease.owner,
        )

    async def run_once(self) -> bool:
        """Claim at most one due timer. Return whether a timer was claimed."""
        if not self.client.is_ready() or not self.presence.ready:
            return False
        await asyncio.to_thread(self.lease.ensure_current)
        claimed = await asyncio.to_thread(
            self.store.claim_due, owner=self.lease.owner, fence=self.lease.fence,
        )
        if claimed is None:
            return False
        status, reason = "unknown", "execution_not_completed"
        try:
            if self.settle_seconds:
                await asyncio.sleep(self.settle_seconds)
            status, reason = await self._decide_and_act(claimed)
        except SingleWriterLeaseLost:
            # New fence owns recovery; this owner must not finalize its claim.
            raise
        except Exception:
            logger.exception("sleep timer Discord result uncertain id=%s", claimed["_id"])
            status, reason = "unknown", "discord_result_uncertain"
        await asyncio.to_thread(self.lease.ensure_current)
        finished = await asyncio.to_thread(
            self.store.finish, claimed, owner=self.lease.owner, fence=self.lease.fence,
            status=status, reason=reason,
        )
        if not finished:
            logger.error("sleep timer final CAS rejected id=%s", claimed["_id"])
            raise RuntimeError("sleep timer final CAS rejected")
        logger.info("sleep timer finished id=%s status=%s reason=%s", claimed["_id"], status, reason)
        return True

    async def _decide_and_act(self, claimed: dict[str, Any]) -> tuple[str, str]:
        guild_id = claimed["guildId"]
        user_id = claimed["targetUserId"]
        if self.allowed_guild_id and guild_id != self.allowed_guild_id:
            return "skipped", "guild_not_allowed"
        if not self.client.is_ready() or not self.presence.ready:
            return "unknown", "gateway_not_ready"
        guild = self.client.get_guild(int(guild_id))
        if guild is None or getattr(guild, "unavailable", False):
            return "unknown", "guild_unavailable"
        member = guild.get_member(int(user_id))
        if member is None:
            return "skipped", "member_not_found"
        channel = _live_channel(member)
        if channel is None:
            return "skipped", "not_in_voice"
        channel_id = str(channel.id)
        if not await self.presence.proves_presence(
            guild_id, user_id, claimed["dueAt"], channel_id,
            gateway_ready=self.client.is_ready(), lease_check=self.lease.ensure_current,
        ):
            return "unknown", "presence_at_deadline_unproved"
        if not _bot_can_move(guild, channel, self.client):
            return "failed", "move_members_permission_missing"
        # Discord provides no conditional member move. Minimize the race by
        # rechecking both local evidence and live cache immediately before REST.
        refreshed = guild.get_member(int(user_id))
        refreshed_channel = _live_channel(refreshed)
        if refreshed_channel is None or str(refreshed_channel.id) != channel_id:
            return "unknown", "voice_state_changed_before_action"
        if not await self.presence.proves_presence(
            guild_id, user_id, claimed["dueAt"], channel_id,
            gateway_ready=self.client.is_ready(), lease_check=self.lease.ensure_current,
        ):
            return "unknown", "presence_changed_before_action"
        await asyncio.to_thread(self.lease.ensure_current)
        return await self._disconnect_once(guild_id, user_id)

    async def _disconnect_once(self, guild_id: str, user_id: str) -> tuple[str, str]:
        """Use one raw PATCH: discord.py HTTPClient retries ambiguous 5xx/IO."""
        url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {
            "Authorization": f"Bot {self.discord_token}",
            "X-Audit-Log-Reason": "Scheduled voice sleep timer",
        }
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.patch(
                url, json={"channel_id": None}, allow_redirects=False,
            ) as response:
                if 200 <= response.status < 300:
                    return "disconnected", "discord_patch_succeeded"
                if response.status == 404:
                    return "skipped", "discord_member_not_found"
                if response.status == 403:
                    return "failed", "discord_forbidden"
                if response.status == 429:
                    return "failed", "discord_rate_limited_no_retry"
                if response.status >= 500:
                    return "unknown", "discord_server_result_uncertain"
                return "failed", f"discord_http_{response.status}"
