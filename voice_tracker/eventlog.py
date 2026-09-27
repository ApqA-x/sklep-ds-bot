"""T09: durable event log — Mongo outbox + per-consumer inbox поверх Core NATS.

Транспорт остаётся Core NATS (низколатентный fast path), но достоверность источника
переносится в Mongo, где уже живут эффекты и бэкапы (ADR-0002):

event_log  — устойчивый намерен-факт издателя (outbox): строка пишется ДО вызова
             транспорта; retry/republish переиспользует тот же event_id (T09.4).
event_inbox — состояние обработки конкретным consumer'ом (per-consumer scope, E05):
             received → processing(lease+fence) → completed | quarantined.
             Ack = completed после устойчивого результата хендлера (T09.5);
             transient failure освобождает lease для следующей попытки (E03);
             исчерпание max_deliver или poison → карантин с причиной (E08).
event_sweep_state — курсор догрузки consumer'а (R26-01): позиция (createdAt, _id)
             последней просмотренной строки журнала. Продвижение только вперёд;
             незавершённые за курсором добирает retry-проход по inbox, а не
             повторный скан префикса журнала.

Sweep (sweep_pending, R26-01/E01) — три прохода за тик:
  forward — события СТРОГО за курсором, сортировка (createdAt,_id), страница
            bounded (scan_limit). Курсор двигается за последний просмотренный ряд,
            у которого есть inbox-строка любого состояния (completed/quarantined —
            терминальны; processing/received — их завершением владеет inbox-путь);
  retry   — inbox-строки consumer'а в received / истёкший processing независимо
            от курсора: повторные попытки старых событий не исчезают за
            high-water mark;
  gap     — окно (курсор − gap_seconds, курсор]: ловит строки, вставленные с
            «опоздавшим» createdAt (задержка записи/часов).
Каждый проход ограничен по числу операций и памяти; весь синхронный Mongo I/O
исполняется через asyncio.to_thread (L06: тик не блокирует event loop).
pending_stats — несколько indexed count/limit-1 запросов вместо полного N+1
обхода журнала; missing/received/expired processing в backlog, active processing
и карантин — отдельными полями.
"""
from __future__ import annotations

import asyncio
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
COLL_SWEEP_STATE = "event_sweep_state"

STATE_RECEIVED = "received"
STATE_PROCESSING = "processing"
STATE_COMPLETED = "completed"
STATE_QUARANTINED = "quarantined"

DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_DELIVER = 8
# R26-01: потолок строк страницы за тик и ширина окна опозданий вставки.
DEFAULT_SCAN_LIMIT = 2000
DEFAULT_GAP_SECONDS = 120.0


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
    eid = await asyncio.to_thread(record, db, subject, payload, event_id=event_id)
    try:
        await bus.publish_json(subject, value, message_id=eid)
        await asyncio.to_thread(
            db[COLL_EVENT_LOG].update_one,
            {"_id": eid}, {"$set": {"publishedAt": _utc_now(), "publishError": None}},
        )
    except Exception as err:  # noqa: BLE001 — журнал уже устойчив, транспорт догонит
        await asyncio.to_thread(
            db[COLL_EVENT_LOG].update_one,
            {"_id": eid}, {"$set": {"publishedAt": None, "publishError": str(err)[:300]}},
        )
        logger.warning("event publish deferred subject=%s id=%s: %s", subject, eid, err)
    return eid


def _unpublished_rows(db: Any, subject: str, limit: int) -> list[dict]:
    return list(
        db[COLL_EVENT_LOG].find({"subject": subject, "publishedAt": None}).sort([("createdAt", 1)]).limit(limit)
    )


async def republish_pending(bus: Any, db: Any, subject: str, *, limit: int = 50) -> int:
    """Повторная доставка устойчивых, но не подтверждённых транспортом событий.
    Тот же event_id → тот же message_id → потребители дедуплицируют (E02)."""
    rows = await asyncio.to_thread(_unpublished_rows, db, subject, limit)
    republished = 0
    for row in rows:
        try:
            await bus.publish_json(subject, row["payload"], message_id=row["_id"])
            await asyncio.to_thread(
                db[COLL_EVENT_LOG].update_one,
                {"_id": row["_id"]}, {"$set": {"publishedAt": _utc_now(), "publishError": None}},
            )
            republished += 1
        except Exception as err:  # noqa: BLE001 — попробуем в следующем свипе
            await asyncio.to_thread(
                db[COLL_EVENT_LOG].update_one,
                {"_id": row["_id"]}, {"$set": {"publishError": str(err)[:300]}},
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
    гонка «доставка по сети + догрузка из журнала» не даёт двойного эффекта.

    Результат complete() проверяется: потерянный при takeover fence НЕ отдаётся как
    «completed» — иначе caller удвоил бы эффект, считая доставку подтверждённой."""
    claimed = await asyncio.to_thread(claim, db, event_id, consumer, subject)
    if claimed is None:
        return "skipped"
    try:
        result = handler(payload)
        if inspect.isawaitable(result):
            await result
    except Exception as err:  # noqa: BLE001 — классификация в fail_attempt
        return await asyncio.to_thread(
            fail_attempt, db, claimed, f"{type(err).__name__}: {err}", max_deliver=max_deliver
        )
    done = await asyncio.to_thread(complete, db, claimed)
    if not done:
        logger.warning(
            "inbox fence lost at complete (takeover during handler) inbox=%s consumer=%s event=%s",
            claimed.inbox_id,
            consumer,
            event_id,
        )
        return "fence_lost"
    return "completed"


# ------------------------------------------------------------- sweep checkpoint


def _state_key(consumer: str, subjects: list[str]) -> str:
    digest = hashlib.sha256("\x1f".join([consumer, *sorted(subjects)]).encode("utf-8")).hexdigest()
    return f"{consumer}\x1f{digest[:24]}"


def _load_position(db: Any, key: str) -> tuple[datetime, str] | None:
    """Позиция курсора (createdAt, _id). Повреждённая (не-datetime/не-строка) —
    трактуется как «курсора нет»: полный replay, идемпотентность держит inbox."""
    doc = db[COLL_SWEEP_STATE].find_one({"_id": key})
    if not doc:
        return None
    position = doc.get("position") or {}
    created = _as_utc(position.get("createdAt"))
    event_id = position.get("eventId")
    if created is None or not isinstance(event_id, str):
        if doc.get("position") is not None:
            logger.warning("sweep checkpoint corrupted key=%s — полный replay с начала журнала", key)
        return None
    return created, event_id


def _forward_filter(subjects: list[str], position: tuple[datetime, str] | None) -> dict:
    base: dict[str, Any] = {"subject": {"$in": list(subjects)}}
    if position is None:
        return base
    created, event_id = position
    base["$or"] = [
        {"createdAt": {"$gt": created}},
        {"createdAt": created, "_id": {"$gt": event_id}},
    ]
    return base


def _save_position(db: Any, key: str, row_created: datetime, row_id: str, subjects: list[str]) -> None:
    """Advance-only: повторная запись с той же/более старой позицией безвредна
    (идемпотентно), более новая не перетирается фильтром $or ниже."""
    now = _utc_now()
    res = db[COLL_SWEEP_STATE].update_one(
        {
            "_id": key,
            "$or": [
                {"position": None},
                {"position.createdAt": {"$lt": row_created}},
                {"position.createdAt": row_created, "position.eventId": {"$lt": row_id}},
            ],
        },
        {
            "$set": {
                "position": {"createdAt": row_created, "eventId": row_id},
                "subjects": sorted(subjects),
                "updatedAt": now,
            }
        },
    )
    if res.matched_count == 1:
        return
    existing = db[COLL_SWEEP_STATE].find_one({"_id": key}, {"position": 1})
    if existing is None:
        try:
            db[COLL_SWEEP_STATE].insert_one(
                {
                    "_id": key,
                    "position": {"createdAt": row_created, "eventId": row_id},
                    "subjects": sorted(subjects),
                    "createdAt": now,
                    "updatedAt": now,
                }
            )
        except Exception as err:  # noqa: BLE001 — гонка: победил другой advance
            if not _is_duplicate(err):
                raise
        return
    position = existing.get("position") or {}
    if _as_utc(position.get("createdAt")) is None or not isinstance(position.get("eventId"), str):
        # повреждённый курсор (не-datetime/не-строка): advance-фильтр по нему не
        # сравнивает — чиним безусловно, иначе replay-позиция не восстановится никогда
        db[COLL_SWEEP_STATE].update_one(
            {"_id": key},
            {
                "$set": {
                    "position": {"createdAt": row_created, "eventId": row_id},
                    "subjects": sorted(subjects),
                    "updatedAt": now,
                }
            },
        )


def _fetch_forward_page(
    db: Any, subjects: list[str], position: tuple[datetime, str] | None, limit: int
) -> list[dict]:
    return list(
        db[COLL_EVENT_LOG]
        .find(_forward_filter(subjects, position), {"subject": 1, "createdAt": 1})
        .sort([("createdAt", 1), ("_id", 1)])
        .limit(limit)
    )


def _inbox_projection(db: Any, consumer: str, event_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not event_ids:
        return out
    ids = [inbox_id(e, consumer) for e in event_ids]
    for doc in db[COLL_EVENT_INBOX].find(
        {"_id": {"$in": ids}},
        {"state": 1, "leaseExpiresAt": 1, "attempts": 1, "eventId": 1, "subject": 1},
    ):
        out[doc["_id"]] = doc
    return out


def _retry_candidates(db: Any, consumer: str, subjects: list[str], now: datetime, limit: int) -> list[dict]:
    """Retry-проход: незавершённые inbox-строки consumer'а (received или истёкший
    processing) — они за курсором навсегда, и без этого прохода повторная попытка
    исчезла бы за high-water mark (R26-01.2)."""
    cursor = db[COLL_EVENT_INBOX].find(
        {
            "consumer": consumer,
            "subject": {"$in": list(subjects)},
            "$or": [
                {"state": STATE_RECEIVED},
                {"state": STATE_PROCESSING, "leaseExpiresAt": {"$lte": now}},
            ],
        },
        {"eventId": 1, "subject": 1, "state": 1, "leaseExpiresAt": 1},
    ).sort([("createdAt", 1)]).limit(limit)
    return list(cursor)


def _full_event(db: Any, event_id: str) -> dict | None:
    return db[COLL_EVENT_LOG].find_one({"_id": event_id}, {"subject": 1, "payload": 1})


def _quarantine_orphan(db: Any, consumer: str, row: dict, error: str) -> str:
    """Inbox-строка указывает на отсутствующий в журнале event (удалённый/повреждённый
    курсор-источник): claim + карантин с причиной — bounded, наблюдаемо, не вечно."""
    claimed = claim(db, row["eventId"], consumer, row.get("subject", ""))
    if claimed is None:
        return "skipped"
    return fail_attempt(db, claimed, error, poison=True)


def _payload_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _pending_stats_sync(db: Any, consumer: str, subjects: list[str], *, cap: int = 10000) -> dict[str, Any]:
    """Бюджетная метрика (R26-01.5/6): несколько indexed запросов вместо обхода
    всего журнала с find_one на строку. pending = реальная незавершённая работа:
    missing inbox (анти-join, ограниченный потолком cap) + received + истёкший
    processing; активные попытки и карантин — отдельными полями."""
    now = _utc_now()
    key = _state_key(consumer, subjects)
    position = _load_position(db, key)
    fwd = _forward_filter(subjects, position)
    inbox_coll = db[COLL_EVENT_INBOX]
    # один агрегат: страница за курсором (индекс subject+createdAt+_id), correlated
    # lookup inbox consumer'а, считаем строки без inbox-строки и самую старую из них
    missing = 0
    oldest_missing: datetime | None = None
    pipeline = [
        {"$match": fwd},
        {"$sort": {"createdAt": 1, "_id": 1}},
        {"$limit": cap},
        {
            "$lookup": {
                "from": COLL_EVENT_INBOX,
                "let": {"eid": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$eventId", "$$eid"]},
                                    {"$eq": ["$consumer", consumer]},
                                ]
                            }
                        }
                    },
                    {"$limit": 1},
                ],
                "as": "_mine",
            }
        },
        {"$match": {"_mine": {"$size": 0}}},
        {
            "$facet": {
                "count": [{"$count": "c"}],
                "oldest": [{"$group": {"_id": None, "oldest": {"$min": "$createdAt"}}}],
            }
        },
    ]
    for res in db[COLL_EVENT_LOG].aggregate(pipeline):
        facet = res or {}
        counts = facet.get("count") or []
        missing = int(counts[0]["c"]) if counts else 0
        oldest_rows = facet.get("oldest") or []
        if oldest_rows:
            oldest_missing = _as_utc(oldest_rows[0].get("oldest"))
    received = inbox_coll.count_documents({"consumer": consumer, "state": STATE_RECEIVED})
    expired = inbox_coll.count_documents(
        {"consumer": consumer, "state": STATE_PROCESSING, "leaseExpiresAt": {"$lte": now}}
    )
    active = inbox_coll.count_documents(
        {"consumer": consumer, "state": STATE_PROCESSING, "leaseExpiresAt": {"$gt": now}}
    )
    completed = inbox_coll.count_documents({"consumer": consumer, "state": STATE_COMPLETED})
    quarantined = inbox_coll.count_documents({"consumer": consumer, "state": STATE_QUARANTINED})
    oldest = oldest_missing
    retry_filter = {
        "consumer": consumer,
        "$or": [
            {"state": STATE_RECEIVED},
            {"state": STATE_PROCESSING, "leaseExpiresAt": {"$lte": now}},
        ],
    }
    for row in inbox_coll.find(retry_filter).sort([("createdAt", 1)]).limit(1):
        created = _as_utc(row.get("createdAt"))
        if created is not None and (oldest is None or created < oldest):
            oldest = created
        break
    backlog = missing + received + expired
    return {
        "consumer": consumer,
        "processed": completed,
        "quarantined": quarantined,
        "backlog": backlog,
        "missingInbox": missing,
        "missingCapped": missing >= cap,
        "received": received,
        "expiredProcessing": expired,
        "activeProcessing": active,
        "oldestPendingAgeSeconds": (now - oldest).total_seconds() if oldest else 0.0,
    }


async def sweep_pending(
    db: Any,
    consumer: str,
    subjects: list[str],
    handler: Handler,
    *,
    limit: int = 200,
    max_deliver: int = DEFAULT_MAX_DELIVER,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
) -> int:
    """E01/R26-01: полная догрузка журнала. Возвращает число реально исполненных
    (закейченных) доставок за тик. Продвижение — курсором (createdAt,_id) через
    event_sweep_state; страницы и число DB-операций за тик ограничены; I/O вне
    event loop (to_thread)."""
    key = _state_key(consumer, subjects)
    now = _utc_now()
    position = await asyncio.to_thread(_load_position, db, key)
    delivered = 0

    # 1) retry-проход: non-terminal inbox consumer'а, независимо от курсора.
    retries = await asyncio.to_thread(_retry_candidates, db, consumer, subjects, now, limit)
    retry_budget = limit
    for row in retries:
        if retry_budget <= 0:
            break
        retry_budget -= 1
        event = await asyncio.to_thread(_full_event, db, row["eventId"])
        if event is None:
            await asyncio.to_thread(
                _quarantine_orphan, db, consumer, row, "orphan inbox: event absent from journal"
            )
            continue
        outcome = await deliver(
            db, consumer, row["eventId"], event.get("subject", row.get("subject", "")),
            _payload_bytes(event["payload"]), handler, max_deliver=max_deliver,
        )
        if outcome != "skipped":
            delivered += 1

    # 2) forward-проход: страница за курсором; терминальные/активные inbox-строки
    # пропускаются пакетно (без per-row find_one), курсор двигается за последний
    # просмотренный ряд с inbox-строкой.
    page = await asyncio.to_thread(_fetch_forward_page, db, subjects, position, scan_limit)
    if page:
        existing = await asyncio.to_thread(_inbox_projection, db, consumer, [r["_id"] for r in page])
        last_ok: dict | None = None
        forward_budget = limit
        for row in page:
            doc = existing.get(inbox_id(row["_id"], consumer))
            if doc is not None:
                state = doc.get("state")
                if state in (STATE_COMPLETED, STATE_QUARANTINED):
                    last_ok = row
                    continue
                if state == STATE_PROCESSING:
                    expires = _as_utc(doc.get("leaseExpiresAt"))
                    if expires is None or expires > _utc_now():
                        last_ok = row  # активная попытка: завершение гарантирует retry-проход
                        continue
                # received / истёкший processing → deliver решит по CAS
            if forward_budget <= 0:
                break
            forward_budget -= 1
            event = await asyncio.to_thread(_full_event, db, row["_id"])
            if event is None:  # журнал меняется между страницей и чтением — повторим в след. тик
                break
            outcome = await deliver(
                db, consumer, row["_id"], event.get("subject", row["subject"]),
                _payload_bytes(event["payload"]), handler, max_deliver=max_deliver,
            )
            if outcome == "skipped":
                last_ok = row
                continue
            delivered += 1
            last_ok = row
        if last_ok is not None:
            await asyncio.to_thread(
                _save_position, db, key, _as_utc(last_ok["createdAt"]) or _utc_now(), str(last_ok["_id"]), subjects
            )

    # 3) gap-проход: окно (курсор − gap_seconds, курсор] — опоздавшие вставки с
    # «старым» createdAt; для них нет inbox-строки → forward их уже не увидит.
    position_after = await asyncio.to_thread(_load_position, db, key)
    if position_after is not None and gap_seconds > 0:
        created, event_id = position_after
        gap_filter = {
            "subject": {"$in": list(subjects)},
            "createdAt": {"$gt": created - timedelta(seconds=gap_seconds), "$lte": created},
        }
        window = await asyncio.to_thread(
            lambda: list(
                db[COLL_EVENT_LOG].find(gap_filter, {"subject": 1, "createdAt": 1})
                .sort([("createdAt", 1), ("_id", 1)])
                .limit(scan_limit)
            )
        )
        if window:
            known = {r["_id"] for r in page}
            need = [r for r in window if r["_id"] not in known]
            if need:
                docs = await asyncio.to_thread(_inbox_projection, db, consumer, [r["_id"] for r in need])
                for row in need:
                    if inbox_id(row["_id"], consumer) in docs:
                        continue
                    if delivered >= limit:
                        break
                    event = await asyncio.to_thread(_full_event, db, row["_id"])
                    if event is None:
                        continue
                    outcome = await deliver(
                        db, consumer, row["_id"], event.get("subject", row["subject"]),
                        _payload_bytes(event["payload"]), handler, max_deliver=max_deliver,
                    )
                    if outcome != "skipped":
                        delivered += 1
    return delivered


def pending_stats(db: Any, consumer: str, subjects: list[str]) -> dict[str, Any]:
    """E01 «пропуск не маскируется» + R26-01.5: честный backlog без N+1 обхода.
    Синхронная функция (тесты и вызывающий код дергают напрямую); из event loop
    сервисов вызывается через asyncio.to_thread."""
    return _pending_stats_sync(db, consumer, subjects)


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


def _is_duplicate(err: Exception) -> bool:
    if getattr(err, "code", None) == 11000:
        return True
    return "duplicatekey" in type(err).__name__.lower()


def _plain(value: Any) -> Any:
    from .domain import to_jsonable

    return to_jsonable(value)
