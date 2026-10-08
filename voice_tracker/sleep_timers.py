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
MAX_PENDING_AUDIT = 4096
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


def _restore_utc(value: Any) -> Any:
    """PyMongo's default codec returns UTC instants as naive datetimes."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, dict):
        return {key: _restore_utc(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_utc(item) for item in value]
    return value


def _audit_event(
    timer: dict[str, Any], *, at: datetime, status: str, reason: str,
    event_id: str, actor_user_id: str | None = None,
    source: str | None = None, due_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "eventId": event_id,
        "guildId": timer["guildId"],
        "targetUserId": timer["targetUserId"],
        "actorUserId": actor_user_id or timer.get("actorUserId", ""),
        "source": source or timer.get("source", ""),
        "status": status,
        "reason": reason,
        "dueAt": due_at if due_at is not None else timer.get("dueAt"),
        "at": at,
    }


class SleepTimerStore:
    def __init__(self, collection: Any) -> None:
        self.collection = collection

    def get(self, guild_id: str, target_user_id: str) -> dict[str, Any] | None:
        key = f"{_validate_id(guild_id, 'guild_id')}:{_validate_id(target_user_id, 'target_user_id')}"
        doc = self.collection.find_one({"_id": key})
        return _restore_utc(doc) if doc else None

    def claim_due(self, *, owner: str, fence: int, now: datetime | None = None) -> dict[str, Any] | None:
        """One-shot claim. An executing timer is never claimed a second time."""
        from pymongo import ReturnDocument

        owner = _validate_request_id(owner)
        if not isinstance(fence, int) or fence < 1:
            raise ValueError("positive gateway fence required")
        at = _now(now)
        claimed = self.collection.find_one_and_update(
            {"status": "pending", "dueAt": {"$lte": at}},
            {"$set": {
                "status": "executing", "claimOwner": owner, "claimFence": fence,
                "claimedAt": at, "updatedAt": at,
            }, "$inc": {"revision": 1}},
            sort=[("dueAt", 1), ("_id", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return _restore_utc(claimed) if claimed else None

    def finish(
        self, claimed: dict[str, Any], *, owner: str, fence: int,
        status: str, reason: str, now: datetime | None = None,
    ) -> bool:
        if status not in {"disconnected", "skipped", "failed", "unknown"}:
            raise ValueError("invalid final status")
        at = _now(now)
        audit = _audit_event(
            claimed, at=at, status=status, reason=reason,
            event_id=f"sleep:{claimed['_id']}:revision:{claimed['revision'] + 1}",
        )
        result = self.collection.update_one(
            {
                "_id": claimed["_id"], "revision": claimed["revision"],
                "status": "executing", "claimOwner": owner, "claimFence": fence,
            },
            {"$set": {
                "status": status, "reason": str(reason)[:256],
                "resultAt": at, "updatedAt": at,
            }, "$inc": {"revision": 1}, "$push": {"auditPending": audit}},
        )
        return result.matched_count == 1

    def mark_stale_unknown(
        self, *, current_fence: int, current_owner: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """A successor or restarted local loop never retries an uncertain call."""
        at = _now(now)
        modified = 0
        # A per-document CAS gives each uncertain result a deterministic audit
        # event in the *same* atomic update as its final state.
        for raw in self.collection.find({"status": "executing"}):
            claimed = _restore_utc(raw)
            stale = int(claimed.get("claimFence") or 0) < current_fence
            local = current_owner is not None and claimed.get("claimOwner") == current_owner
            if not stale and not local:
                continue
            reason = "gateway_restarted_during_execution" if stale else "worker_loop_restarted_during_execution"
            audit = _audit_event(
                claimed, at=at, status="unknown", reason=reason,
                event_id=f"sleep:{claimed['_id']}:revision:{claimed['revision'] + 1}",
            )
            result = self.collection.update_one(
                {"_id": claimed["_id"], "revision": claimed["revision"],
                 "status": "executing", "claimOwner": claimed.get("claimOwner"),
                 "claimFence": claimed.get("claimFence")},
                {"$set": {"status": "unknown", "reason": reason,
                          "resultAt": at, "updatedAt": at},
                 "$inc": {"revision": 1}, "$push": {"auditPending": audit}},
            )
            modified += result.matched_count
        return modified

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
            old = _restore_utc(self.collection.find_one({"_id": key}))
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
            audit_pending = list((old or {}).get("auditPending") or [])
            if len(audit_pending) >= MAX_PENDING_AUDIT:
                raise SleepTimerConflict("timer audit backlog full; retry after recovery")
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
                "auditPending": [*audit_pending, _audit_event(
                    new, at=now, status=status, reason=action,
                    event_id=f"sleep:{key}:request:{request_id}",
                    actor_user_id=actor_user_id, source=source, due_at=due_at,
                )],
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
