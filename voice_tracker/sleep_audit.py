"""Recoverable projection of atomic timer events into the site audit log."""

from __future__ import annotations

from itertools import islice
from typing import Any


def _audit_document(event: dict[str, Any]) -> dict[str, Any]:
    reason = event["reason"]
    action = f"sleep.{reason}" if reason in {"set", "cancel"} else "sleep.execute"
    status = event["status"]
    return {
        "_id": event["eventId"],
        "guildId": event["guildId"],
        "actorUserId": event["actorUserId"],
        "actorName": event["actorUserId"],
        "action": action,
        "before": None,
        "after": {
            "targetUserId": event["targetUserId"],
            "status": status,
            "dueAt": event.get("dueAt"),
            "reason": reason,
            "source": event.get("source"),
        },
        "ok": status in {"pending", "cancelled", "disconnected", "skipped"},
        "at": event["at"],
        "source": "web",
        "origin": f"sleep-{event.get('source') or 'gateway'}",
        "stage": "effect" if status in {"pending", "cancelled", "disconnected"} else "invocation",
    }


def project_pending(db: Any, *, limit: int = 100) -> int:
    """At-least-once projector; deterministic audit IDs make replay harmless.

    The timer mutation/decision and its pending event are one Mongo write. If
    audit insertion or acknowledgement fails, the event remains for the next
    sweep. An acknowledgement only removes the exact event already projected.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000")
    timers = db["voice_sleep_timers"]
    audit = db["web_audit_logs"]
    projected = 0
    for timer in islice(timers.find({"auditPending.eventId": {"$exists": True}}), limit):
        for event in timer.get("auditPending") or []:
            audit.update_one(
                {"_id": event["eventId"]},
                {"$setOnInsert": _audit_document(event)}, upsert=True,
            )
            timers.update_one(
                {"_id": timer["_id"]},
                {"$pull": {"auditPending": event}},
            )
            projected += 1
    return projected
