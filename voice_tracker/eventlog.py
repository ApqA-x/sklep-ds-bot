"""T09: durable event log — Mongo outbox + per-consumer inbox поверх Core NATS.

Транспорт остаётся Core NATS (низколатентный fast path), но достоверность источника
переносится в Mongo, где уже живут эффекты и бэкапы (ADR-0002):

event_log  — устойчивый намерен-факт издателя (outbox): строка пишется ДО вызова
             транспорта; retry/republish переиспользует тот же event_id (T09.4).
event_inbox — состояние обработки конкретным consumer'ом (per-consumer scope, E05):
             received → processing(lease+fence) → completed | quarantined.
             Ack = completed после устойчивого результата хендлера (T09.5);
             transient failure освобождает lease для следующей попытки (E03);
             исчерпание max_deliver или poison → quarantine с причиной (E08).

Sweep (sweep_pending) — «доставка из журнала»: consumer, пропустивший wire-момент
(NATS upsert-only, рестарт сервиса, простой), добирает необработанные события по
createdAt — это E01/E02 без JetStream-тома и без переключения транспорта.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

logger = logging.getLogger(__name__)

COLL_EVENT_LOG = "event_log"
COLL_EVENT_INBOX = "event_inbox"

STATE_RECEIVED = "received"
STATE_PROCESSING = "processing"
STATE_COMPLETED = "completed"
STATE_QUARANTINED = "quarantined"

DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_DELIVER = 8


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: object) -> datetime | None:
    # BSON datetime приезжает наивным UTC (tz_aware=False у MongoClient) — нормализуем
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def new_event_id() -> str:
    return uuid4().hex


def deterministic_event_id(kind: str, key: str) -> str:
    """T09.4: устойчивое событие, порождённое фактом БД, получает стабильный id —
    повторная публикация того же факта не создаёт второго события."""
    return hashlib.sha256(f"{kind}\x1f{key}".encode("utf-8")).hexdigest()


def inbox_id(event_id: str, consumer: str) -> str:
    return hashlib.sha256(f"{event_id}\x1f{consumer}".encode("utf-8")).hexdigest()


def poison_id(raw: bytes) -> str:
    return hashlib.sha256(b"poison\x1f" + bytes(raw)).hexdigest()


# ---------------------------------------------------------------- publisher side


def record(
    db: Any,
    subject: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    issuer: str = "",
) -> str:
    """Устойчивое намерение публикации (T09.4): строка журнала ДО любого транспорта."""
    eid = event_id or new_event_id()
    now = _utc_now()
    try:
        db[COLL_EVENT_LOG].insert_one(
            {
                "_id": eid,
                "subject": subject,
                "issuer": issuer,
                "payload": payload,
                "createdAt": now,
                "publishedAt": None,
                "publishError": None,
            }
        )
    except Exception as err:  # noqa: BLE001 — duplicate _id = уже записано (retry)
        if not _is_duplicate(err):
            raise
    return eid


async def publish_durable(
    bus: Any,
    db: Any,
    subject: str,
    value: Any,
    *,
    event_id: str | None = None,
) -> str:
    """Записать в журнал и попробовать опубликовать. Сбой транспорта не теряет
    событие: строка остаётся с publishedAt=None и будет повторена republish_pending
    с тем же message_id (T09.4)."""
    payload = value if isinstance(value, dict) else json.loads(
        json.dumps(_plain(value), ensure_ascii=False, default=str)
    )
    eid = record(db, subject, payload, event_id=event_id)
    try:
        await bus.publish_json(subject, value, message_id=eid)
        db[COLL_EVENT_LOG].update_one(
            {"_id": eid}, {"$set": {"publishedAt": _utc_now(), "publishError": None}}
        )
    except Exception as err:  # noqa: BLE001 — журнал уже устойчив, транспорт догонит
        db[COLL_EVENT_LOG].update_one(
            {"_id": eid}, {"$set": {"publishedAt": None, "publishError": str(err)[:300]}}
        )
        logger.warning("event publish deferred subject=%s id=%s: %s", subject, eid, err)
    return eid


async def republish_pending(bus: Any, db: Any, subject: str, *, limit: int = 50) -> int:
    """Повторная доставка устойчивых, но не подтверждённых транспортом событий.
    Тот же event_id → тот же message_id → потребители дедуплицируют (E02)."""
    rows = list(
        db[COLL_EVENT_LOG].find({"subject": subject, "publishedAt": None}).sort("createdAt", 1).limit(limit)
    )
    republished = 0
    for row in rows:
        try:
            await bus.publish_json(subject, row["payload"], message_id=row["_id"])
            db[COLL_EVENT_LOG].update_one(
                {"_id": row["_id"]}, {"$set": {"publishedAt": _utc_now(), "publishError": None}}
            )
            republished += 1
        except Exception as err:  # noqa: BLE001 — попробуем в следующем свипе
            db[COLL_EVENT_LOG].update_one(
                {"_id": row["_id"]}, {"$set": {"publishError": str(err)[:300]}}
            )
            logger.warning("event republish failed subject=%s id=%s: %s", subject, row["_id"], err)
            break
    return republished


# ---------------------------------------------------------------- consumer side


@dataclass(slots=True)
class Claim:
    inbox_id: str
    event_id: str
    consumer: str
    subject: str
    attempts: int
    lease_token: str


def claim(db: Any, event_id: str, consumer: str, subject: str) -> Claim | None:
    """Атомарный claim в per-consumer scope (E05). None — событие уже обработано
    этим consumer'ом, в карантине, или его ведёт активная попытка (lease не истёк).

    Истёкший lease перехватывается CAS-ом; попытка не считает effect доказанным —
    хендлер обязан быть идемпотентным (T09.6/E04)."""
    iid = inbox_id(event_id, consumer)
    now = _utc_now()
    token = uuid4().hex
    doc = {
        "_id": iid,
        "eventId": event_id,
        "consumer": consumer,
        "subject": subject,
        "state": STATE_PROCESSING,
        "attempts": 1,
        "leaseToken": token,
        "leaseExpiresAt": now + timedelta(seconds=DEFAULT_LEASE_SECONDS),
        "lastError": None,
        "createdAt": now,
        "updatedAt": now,
        "completedAt": None,
    }
    try:
        db[COLL_EVENT_INBOX].insert_one(dict(doc))
        return Claim(iid, event_id, consumer, subject, 1, token)
    except Exception as err:  # noqa: BLE001
        if not _is_duplicate(err):
            raise
    existing = db[COLL_EVENT_INBOX].find_one({"_id": iid})
    if existing is None:
        return None
    state = existing.get("state")
    if state in (STATE_COMPLETED, STATE_QUARANTINED):
        return None
    if state == STATE_PROCESSING:
        expires = _as_utc(existing.get("leaseExpiresAt"))
        if expires is None or expires > now:
            return None  # активная попытка другого исполнителя
        outcome = db[COLL_EVENT_INBOX].update_one(
            {"_id": iid, "state": STATE_PROCESSING, "leaseToken": existing.get("leaseToken")},
            {
                "$set": {
                    "state": STATE_PROCESSING,
                    "leaseToken": token,
                    "leaseExpiresAt": now + timedelta(seconds=DEFAULT_LEASE_SECONDS),
                    "updatedAt": now,
                },
                "$inc": {"attempts": 1},
            },
        )
        if outcome.matched_count != 1:
            return None
        return Claim(iid, event_id, consumer, subject, int(existing.get("attempts", 0)) + 1, token)
    # received: предыдущая попытка освободила lease для retry (E03)
    outcome = db[COLL_EVENT_INBOX].update_one(
        {"_id": iid, "state": STATE_RECEIVED},
        {
            "$set": {
                "state": STATE_PROCESSING,
                "leaseToken": token,
                "leaseExpiresAt": now + timedelta(seconds=DEFAULT_LEASE_SECONDS),
                "updatedAt": now,
            },
            "$inc": {"attempts": 1},
        },
    )
    if outcome.matched_count != 1:
        return None
    return Claim(iid, event_id, consumer, subject, int(existing.get("attempts", 0)) + 1, token)


def complete(db: Any, claimed: Claim) -> bool:
    """Ack после устойчивого результата (T09.5): CAS по lease-token — чужой
    (перехваченный) claim не смаркируется completed."""
    outcome = db[COLL_EVENT_INBOX].update_one(
        {"_id": claimed.inbox_id, "state": STATE_PROCESSING, "leaseToken": claimed.lease_token},
        {
            "$set": {
                "state": STATE_COMPLETED,
                "completedAt": _utc_now(),
                "leaseExpiresAt": None,
                "lastError": None,
                "updatedAt": _utc_now(),
            }
        },
    )
    return outcome.matched_count == 1


def fail_attempt(
    db: Any,
    claimed: Claim,
    error: str,
    *,
    poison: bool = False,
    max_deliver: int = DEFAULT_MAX_DELIVER,
) -> str:
    """Transient failure → received (следующая доставка/свип получит попытку, E03).
    Poison или исчерпание max_deliver → quarantined с причиной (E08)."""
    now = _utc_now()
    state = STATE_QUARANTINED if poison or claimed.attempts >= max_deliver else STATE_RECEIVED
    outcome = db[COLL_EVENT_INBOX].update_one(
        {"_id": claimed.inbox_id, "state": STATE_PROCESSING, "leaseToken": claimed.lease_token},
        {
            "$set": {
                "state": state,
                "lastError": error[:500],
                "leaseToken": None,
                "leaseExpiresAt": None,
                "updatedAt": now,
            }
        },
    )
    if outcome.matched_count != 1:
        logger.warning(
            "inbox fence lost (takeover) inbox=%s consumer=%s attempts=%s",
            claimed.inbox_id,
            claimed.consumer,
            claimed.attempts,
        )
    if state == STATE_QUARANTINED:
        logger.error(
            "event quarantined consumer=%s subject=%s event=%s attempts=%s error=%s",
            claimed.consumer,
            claimed.subject,
            claimed.event_id,
            claimed.attempts,
            error[:300],
        )
    return state


def quarantine_poison(db: Any, consumer: str, subject: str, raw: bytes, reason: str) -> str:
    """E08: нечитаемый/подписной мусор — карантин по хэшу содержимого, с причиной.
    Блокировать очередь не может: у него нет event_id из конверта."""
    event_id = poison_id(raw)
    iid = inbox_id(event_id, consumer)
    now = _utc_now()
    try:
        db[COLL_EVENT_INBOX].insert_one(
            {
                "_id": iid,
                "eventId": event_id,
                "consumer": consumer,
                "subject": subject,
                "state": STATE_QUARANTINED,
                "attempts": 0,
                "leaseToken": None,
                "leaseExpiresAt": None,
                "lastError": reason[:500],
                "createdAt": now,
                "updatedAt": now,
                "completedAt": None,
            }
        )
    except Exception as err:  # noqa: BLE001 — уже в карантине
        if not _is_duplicate(err):
            raise
    logger.error("poison envelope quarantined consumer=%s subject=%s reason=%s", consumer, subject, reason[:300])
    return iid


Handler = Callable[[bytes], Any]


async def deliver(
    db: Any,
    consumer: str,
    event_id: str,
    subject: str,
    payload: bytes,
    handler: Handler,
    *,
    max_deliver: int = DEFAULT_MAX_DELIVER,
) -> str:
    """Единая точка исполнения: wire-путь и sweep идут через один claim, поэтому
    гонка «доставка по сети + догрузка из журнала» не даёт двойного эффекта."""
    claimed = claim(db, event_id, consumer, subject)
    if claimed is None:
        return "skipped"
    try:
        result = handler(payload)
        if inspect.isawaitable(result):
            await result
    except Exception as err:  # noqa: BLE001 — классификация в fail_attempt
        return fail_attempt(db, claimed, f"{type(err).__name__}: {err}", max_deliver=max_deliver)
    complete(db, claimed)
    return "completed"


async def sweep_pending(
    db: Any,
    consumer: str,
    subjects: list[str],
    handler: Handler,
    *,
    limit: int = 200,
    max_deliver: int = DEFAULT_MAX_DELIVER,
) -> int:
    """E01: доставка пропущенных/незавершённых событий из журнала по createdAt.
    Возвращает число реально исполненных (закейченных) доставок."""
    delivered = 0
    rows = (
        db[COLL_EVENT_LOG].find({"subject": {"$in": subjects}}).sort("createdAt", 1).limit(limit)
    )
    for row in rows:
        existing = db[COLL_EVENT_INBOX].find_one({"_id": inbox_id(row["_id"], consumer)})
        if existing is not None:
            state = existing.get("state")
            if state in (STATE_COMPLETED, STATE_QUARANTINED, STATE_PROCESSING):
                if state != STATE_PROCESSING:
                    continue
                expires = _as_utc(existing.get("leaseExpiresAt"))
                if expires is None or expires > _utc_now():
                    continue  # активная попытка не трогаем
            # received или истёкший processing → deliver сам решит по CAS
        payload = json.dumps(row["payload"], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        outcome = await deliver(db, consumer, row["_id"], row["subject"], payload, handler, max_deliver=max_deliver)
        if outcome == "completed":
            delivered += 1
    return delivered


class DurablePublisher:
    """Publisher-обёртка: событие с устойчивым фактом-источником получает
    детерминированный event_id (T09.4) и проходит через outbox."""

    def __init__(self, bus: Any, db: Any, *, issuer: str = "") -> None:
        self.bus = bus
        self.db = db
        self.issuer = issuer

    async def publish_json(self, *args: Any, **kwargs: Any) -> None:
        # совместим и с publish_json(subject, value), и с legacy publish_json(ctx, subject, value)
        if len(args) == 3:
            _, subject, value = args
        elif len(args) == 2:
            subject, value = args
        else:
            raise TypeError("publish_json expects subject/value or ctx/subject/value")
        from .domain import SUBJECT_SESSION_CLOSED, SUBJECT_SUMMARY_READY

        event_id = kwargs.get("event_id")
        if event_id is None and subject == SUBJECT_SUMMARY_READY:
            session_id = str(
                getattr(value, "session_id", "") or (value.get("sessionId") if isinstance(value, dict) else "")
            )
            if session_id:
                event_id = deterministic_event_id("summary-ready", session_id)
        if event_id is None and subject == SUBJECT_SESSION_CLOSED:
            session_id = str(
                getattr(value, "session_id", "") or (value.get("sessionId") if isinstance(value, dict) else "")
            )
            if session_id:
                # T09.4/E04: reaper/tracker могут опубликовать закрытие сессии дважды —
                # это одно устойчивое событие с одним id
                event_id = deterministic_event_id("session-closed", session_id)
        await publish_durable(self.bus, self.db, subject, value, event_id=event_id)


def pending_stats(db: Any, consumer: str, subjects: list[str]) -> dict[str, Any]:
    """E01 «пропуск не маскируется»: измеряемая отсталость потребителя."""
    now = _utc_now()
    processed = db[COLL_EVENT_INBOX].count_documents({"consumer": consumer, "state": STATE_COMPLETED})
    quarantined = db[COLL_EVENT_INBOX].count_documents({"consumer": consumer, "state": STATE_QUARANTINED})
    oldest: datetime | None = None
    backlog = 0
    for subject in subjects:
        for row in db[COLL_EVENT_LOG].find({"subject": subject}).sort("createdAt", 1):
            if db[COLL_EVENT_INBOX].find_one({"_id": inbox_id(row["_id"], consumer)}) is None:
                backlog += 1
                created = _as_utc(row.get("createdAt"))
                if created is not None and (oldest is None or created < oldest):
                    oldest = created
    return {
        "consumer": consumer,
        "processed": processed,
        "quarantined": quarantined,
        "backlog": backlog,
        "oldestPendingAgeSeconds": (now - oldest).total_seconds() if oldest else 0.0,
    }


def _is_duplicate(err: Exception) -> bool:
    if getattr(err, "code", None) == 11000:
        return True
    return "duplicatekey" in type(err).__name__.lower()


def _plain(value: Any) -> Any:
    from .domain import to_jsonable

    return to_jsonable(value)
