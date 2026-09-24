from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

COLL_WEB_AUDIT = "web_audit_logs"

REASON_DISABLED = "disabled"
REASON_PERMISSIONS = "permissions"
REASON_UNKNOWN = "unknown"
REASON_ERROR = "error"
REASON_REJECTED = "rejected"


def build_command_after(
    root: str,
    command: str,
    options: list[Any],
    *,
    channel_id: str = "",
    reason: str = "",
) -> dict[str, Any]:
    route = " ".join(part for part in (f"/{root}", command) if part)
    after: dict[str, Any] = {"command": route}
    arguments = _flatten_options(options)
    if arguments:
        after["options"] = arguments
    if channel_id:
        after["channelId"] = channel_id
    if reason:
        after["reason"] = reason
    return after


def _flatten_options(options: list[Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for option in options or []:
        name = str(getattr(option, "name", "") or "")
        if name == "":
            continue
        children = getattr(option, "options", None) or []
        if children:
            flat.update(_flatten_options(children))
            continue
        value = getattr(option, "value", None)
        if value is None:
            continue
        flat[name] = _audit_value(value)
    return flat


def _audit_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 400:
            return value[:400] + "…"
        return value
    text = str(value)
    if len(text) > 400:
        text = text[:400] + "…"
    return text


def record_command_audit(
    db: Any,
    *,
    guild_id: str,
    actor_user_id: str,
    actor_name: str,
    root: str,
    command: str,
    after: dict[str, Any],
    ok: bool,
) -> None:
    # Document shape mirrors api/mutations.py::record_audit in sklep-ds-bot-web.
    db[COLL_WEB_AUDIT].insert_one(
        {
            "guildId": guild_id,
            "actorUserId": actor_user_id,
            "actorName": actor_name,
            "action": f"command.{root}",
            "before": None,
            "after": after,
            "ok": ok,
            "at": datetime.now(UTC),
            "source": "web",
            "origin": "discord",
        }
    )


def safe_record_command_audit(
    db: Any,
    *,
    guild_id: str,
    actor_user_id: str,
    actor_name: str,
    root: str,
    command: str,
    options: list[Any],
    channel_id: str = "",
    reason: str = "",
    ok: bool = True,
) -> None:
    if db is None or guild_id == "":
        return
    after = build_command_after(root, command, options, channel_id=channel_id, reason=reason)
    try:
        record_command_audit(
            db,
            guild_id=guild_id,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
            root=root,
            command=command,
            after=after,
            ok=ok,
        )
    except Exception:
        logger.warning("site audit write failed guild=%s command=/%s", guild_id, root, exc_info=True)
