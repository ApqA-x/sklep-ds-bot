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

# T08.11 (O10): журнал отличает вызов команды от отказа и от подтверждённого эффекта.
# R26-04 п.6: "Не утверждать подтверждённый effect на основании одного имени mutating
# route" — stage="effect" требует отдельного доказательства (effect_proved), которое
# считается в диспатчере, а не в классификаторе.
STAGE_INVOCATION = "invocation"
STAGE_REJECTED = "rejected"
STAGE_EFFECT = "effect"

# R26-04 п.6: строки-отказы, которые возвращает диспатчер вместо исключения.
# Общий источник истины для services/commands.py (он больше не держит свой словарь).
COMMAND_REJECTION_REASONS: dict[str, str] = {
    "Insufficient permissions.": REASON_PERMISSIONS,
    "Unknown command.": REASON_UNKNOWN,
    "Unknown unmute command.": REASON_UNKNOWN,
    "Unknown trusted command.": REASON_UNKNOWN,
    "Unknown stalker command.": REASON_UNKNOWN,
    "Command failed. Check service logs.": REASON_ERROR,
}

MUTATING_ROUTES = frozenset(
    {
        ("connect", ""),
        ("disconnect", ""),
        ("autorole", ""),
        ("unmute", "add"),
        ("unmute", "remove"),
        ("trusted", "add"),
        ("trusted", "remove"),
        ("stalker", "start"),
        ("stalker", "stop"),
        ("settings", "summary-set"),
        ("settings", "summary-clear"),
        ("settings", "activity-channel-set"),
        ("settings", "activity-channel-clear"),
        ("settings", "activity"),
    }
)


def reject_reason_for_result(result: object) -> str | None:
    if not isinstance(result, str):
        return None
    return COMMAND_REJECTION_REASONS.get(result)


def classify_stage(ok: bool, root: str, command: str, *, effect_proved: bool = False) -> str:
    if not ok:
        return STAGE_REJECTED
    # R26-04 п.6: имя мутирующего маршрута само по себе эффект не доказывает —
    # без effect_proved успешный mutating route остаётся "invocation".
    if effect_proved and (root, command) in MUTATING_ROUTES:
        return STAGE_EFFECT
    return STAGE_INVOCATION


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
    stage: str = STAGE_INVOCATION,
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
            # T08.11: вызов / отказ / подтверждённый эффект — три разных факта в журнале
            "stage": stage,
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
    effect_proved: bool = False,
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
            stage=classify_stage(ok, root, command, effect_proved=effect_proved),
        )
    except Exception:
        logger.warning("site audit write failed guild=%s command=/%s", guild_id, root, exc_info=True)
