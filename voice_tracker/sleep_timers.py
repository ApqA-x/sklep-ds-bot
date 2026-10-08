"""Atomic per-member sleep timer intentions; no Discord action lives here.

One Mongo document is the consistency boundary for a guild/member.  Recent
request IDs and their original outcomes live in that same document, so a crash
cannot split the timer mutation from its idempotency record on standalone Mongo.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


IDEMPOTENCY_DAYS = 90
MAX_RECENT_REQUESTS = 1024
MAX_CAS_ATTEMPTS = 16


class SleepTimerBusy(RuntimeError):
    """A one-shot disconnect decision is already being executed."""


class SleepTimerConflict(RuntimeError):
    """Concurrent mutations did not settle within the bounded CAS loop."""


@dataclass(frozen=True)
class TimerMutation:
    timer: dict[str, Any]
    outcome: dict[str, Any]
    replayed: bool


def _validate_id(value: str, field: str) -> str:
    value = str(value or "").strip()
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise ValueError(f"{field} must be a Discord snowflake")
    return value


def _validate_request_id(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 128:
        raise ValueError("request_id is required (max 128 characters)")
    return value


def _now(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


class SleepTimerStore:
    def __init__(self, collection: Any) -> None:
        self.collection = collection

    def get(self, guild_id: str, target_user_id: str) -> dict[str, Any] | None:
        key = f"{_validate_id(guild_id, 'guild_id')}:{_validate_id(target_user_id, 'target_user_id')}"
        doc = self.collection.find_one({"_id": key})
        return deepcopy(doc) if doc else None

    def set(
        self, guild_id: str, target_user_id: str, hours: int,
        *, actor_user_id: str, source: str, request_id: str,
        now: datetime | None = None,
    ) -> TimerMutation:
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 24:
            raise ValueError("hours must be an integer from 1 to 24")
        return self._mutate(
            guild_id, target_user_id, actor_user_id=actor_user_id,
            source=source, request_id=request_id, action="set", hours=hours,
            now=_now(now),
        )

    def cancel(
        self, guild_id: str, target_user_id: str, *, actor_user_id: str,
        source: str, request_id: str, now: datetime | None = None,
    ) -> TimerMutation:
        return self._mutate(
            guild_id, target_user_id, actor_user_id=actor_user_id,
            source=source, request_id=request_id, action="cancel", hours=None,
            now=_now(now),
        )

    def _mutate(
        self, guild_id: str, target_user_id: str, *, actor_user_id: str,
        source: str, request_id: str, action: str, hours: int | None,
        now: datetime,
    ) -> TimerMutation:
        guild_id = _validate_id(guild_id, "guild_id")
        target_user_id = _validate_id(target_user_id, "target_user_id")
        actor_user_id = _validate_id(actor_user_id, "actor_user_id")
        request_id = _validate_request_id(request_id)
        if source not in {"slash", "web"}:
            raise ValueError("source must be slash or web")
        key = f"{guild_id}:{target_user_id}"
        cutoff = now - timedelta(days=IDEMPOTENCY_DAYS)
        for _ in range(MAX_CAS_ATTEMPTS):
            old = self.collection.find_one({"_id": key})
            history = list((old or {}).get("recentRequests") or [])
            # Check replay before pruning; a duplicate must never change state.
            for entry in history:
                if entry["requestId"] == request_id:
                    if (
                        entry["action"] != action or entry.get("hours") != hours
                        or entry["actorUserId"] != actor_user_id or entry["source"] != source
                    ):
                        raise ValueError("request_id reused with different parameters")
                    return TimerMutation(deepcopy(old), deepcopy(entry), True)
            if old is not None and old.get("status") == "executing":
                raise SleepTimerBusy("timer execution in progress")
            history = [entry for entry in history if entry["at"] >= cutoff]
            if len(history) >= MAX_RECENT_REQUESTS:
                raise SleepTimerConflict("idempotency history full; wait for expiry")
            revision = int((old or {}).get("revision", 0)) + 1
            due_at = now + timedelta(hours=hours) if hours is not None else None
            prior_active = bool(old and old.get("status") == "pending")
            status = "pending" if action == "set" else "cancelled" if prior_active or old is None else old["status"]
            outcome = {
                "requestId": request_id,
                "action": action,
                "hours": hours,
                "actorUserId": actor_user_id,
                "source": source,
                "at": now,
                "revision": revision,
                "status": status,
                "dueAt": due_at if action == "set" else (old or {}).get("dueAt"),
                "hadActiveTimer": prior_active,
            }
            new = deepcopy(old) if old else {"_id": key, "guildId": guild_id, "targetUserId": target_user_id}
            new.update({
                "revision": revision, "status": status,
                "updatedAt": now, "actorUserId": actor_user_id,
                "source": source, "requestId": request_id,
                "recentRequests": [*history, outcome],
            })
            if action == "set":
                new.update({"createdAt": now, "dueAt": due_at, "hours": hours})
                for field in ("claimOwner", "claimFence", "claimedAt", "resultAt", "reason"):
                    new.pop(field, None)
            elif prior_active:
                new["resultAt"] = now
                new["reason"] = "cancelled_by_user"
            if old is None:
                try:
                    self.collection.insert_one(new)
                except Exception as exc:
                    if getattr(exc, "code", None) == 11000 or type(exc).__name__ == "DuplicateKeyError":
                        continue
                    raise
            else:
                result = self.collection.replace_one(
                    {"_id": key, "revision": old["revision"], "status": old["status"]}, new
                )
                if result.matched_count != 1:
                    continue
            return TimerMutation(deepcopy(new), deepcopy(outcome), False)
        raise SleepTimerConflict("concurrent timer updates did not settle")
