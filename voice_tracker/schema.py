"""T10: единый канонический контракт индексов voice_tracker (manifest + сверка).

Манифест — единственный источник правды по индексам обеих сторон (bot и web
пишут в одну БД). Startup приложений: идемпотентное создание своего набора
(без drop!) + строгая сверка спецификаций; массовое пересоздание/удаление —
только миграционный runner (`python -m voice_tracker.migrate`).

Эквивалентность сравнивается по СПЕЦИФИКАЦИИ (ordered keys, unique, partial,
sparse, TTL), а не по имени: индекс под старым именем с теми же ключами и
флагами принимается как эквивалентный и НЕ пересоздаётся (DB01). Одинаковые
ключи под разными флагами — несовместимость, она не скрывается (DB02).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable

SCHEMA_VERSION = 1

# TTL журнала операций (M2): окно replay идемпотентности 90 дней. unknown-записи
# старше окна — история, не состояние; восстановление по ним не требуется.
OPERATIONS_TTL_SECONDS = 90 * 24 * 3600


def auto_index_name(keys: Iterable[tuple[str, int]]) -> str:
    return "_".join(f"{field}_{direction}" for field, direction in keys)


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class IndexSpec:
    collection: str
    keys: tuple[tuple[str, int], ...]
    name: str
    unique: bool = False
    sparse: bool = False
    partial: Any = None  # сериализуемый partialFilterExpression или None
    ttl: int | None = None  # expireAfterSeconds
    owner: str = "bot"  # bot | web | shared | runner

    def key_str(self) -> str:
        return auto_index_name(self.keys)

    def spec_json(self) -> dict:
        return {
            "collection": self.collection,
            "keys": [list(k) for k in self.keys],
            "name": self.name,
            "unique": self.unique,
            "sparse": self.sparse,
            "partial": json.loads(_canon(self.partial)) if self.partial is not None else None,
            "ttl": self.ttl,
            "owner": self.owner,
        }

    def _partial_key(self) -> str:
        return _canon(self.partial) if self.partial is not None else ""

    def create_kwargs(self) -> dict:
        kwargs: dict[str, Any] = {"name": self.name}
        if self.unique:
            kwargs["unique"] = True
        if self.sparse:
            kwargs["sparse"] = True
        if self.partial is not None:
            kwargs["partialFilterExpression"] = self.partial
        if self.ttl is not None:
            kwargs["expireAfterSeconds"] = self.ttl
        return kwargs


def _spec(coll: str, keys: list[tuple[str, int]], owner: str = "bot", *, name: str | None = None,
          unique: bool = False, sparse: bool = False, partial: Any = None, ttl: int | None = None) -> IndexSpec:
    keys_t = tuple(keys)
    return IndexSpec(collection=coll, keys=keys_t, name=name or auto_index_name(keys_t),
                     unique=unique, sparse=sparse, partial=partial, ttl=ttl, owner=owner)


S = "voice_sessions"
P = "voice_session_participants"
PM = "processed_messages"
EL = "event_log"
EI = "event_inbox"
IS = "guild_invite_snapshots"
IC = "invite_catalog"
JA = "member_join_attributions"
JS = "member_join_state"
RS = "member_role_state"
NS = "member_nickname_state"
NH = "member_nickname_history"
SS = "stalker_subscriptions"
CM = "chat_messages"
WA = "web_audit_logs"
DA = "discord_audit_logs"
CP = "chat_presets"
OP = "operations"

MANIFEST: tuple[IndexSpec, ...] = (
    # --- voice_sessions. Индекс active-сессий unique+partial держит инвариант
    # «одна active-сессия на канал» (join/move/restart) — менять только с тестом (T10.7).
    _spec(S, [("status", 1), ("guildId", 1), ("channelId", 1)], unique=True,
          partial={"status": "active"}),
    _spec(S, [("status", 1), ("guildId", 1), ("channelId", 1), ("endedAt", -1)]),
    _spec(S, [("status", 1), ("closedEventPublishedAt", 1)]),
    _spec(S, [("guildId", 1), ("status", 1), ("endedAt", -1)], owner="web",
          name="web_guildId_status_endedAt"),
    # --- participants
    _spec(P, [("sessionId", 1), ("active", 1)]),
    _spec(P, [("guildId", 1), ("sessionId", 1), ("active", 1)]),
    _spec(P, [("guildId", 1), ("channelId", 1), ("sessionId", 1)]),
    _spec(P, [("guildId", 1), ("userId", 1), ("sessionId", 1)]),
    _spec(P, [("guildId", 1), ("userId", 1), ("joinedAt", -1)]),
    _spec(P, [("guildId", 1), ("joinedAt", 1)], owner="web", name="web_guildId_joinedAt"),
    _spec(P, [("sessionId", 1), ("userId", 1), ("active", 1)], unique=True,
          partial={"active": True}),
    # --- processed_messages (легаси-дедуп; TTL 2ч — окно переотправки NATS)
    _spec(PM, [("subject", 1), ("messageId", 1)], unique=True),
    _spec(PM, [("createdAt", 1)], ttl=7200),
    # --- T09 outbox/inbox
    _spec(EL, [("subject", 1), ("createdAt", 1)], name="event_log_subject_createdAt"),
    _spec(EL, [("publishedAt", 1)], name="event_log_unpublished", partial={"publishedAt": None}),
    _spec(EI, [("consumer", 1), ("state", 1)], name="event_inbox_consumer_state"),
    _spec(EI, [("eventId", 1)], name="event_inbox_eventId"),
    # --- invites
    _spec(IS, [("guildId", 1)], unique=True),
    _spec(IS, [("capturedAt", -1)]),
    _spec(IC, [("guildId", 1), ("code", 1)], unique=True),
    _spec(IC, [("guildId", 1), ("lastSeenAt", -1)]),
    _spec(IC, [("guildId", 1), ("deletedAt", 1)]),
    # --- join attribution / states / history
    _spec(JA, [("guildId", 1), ("userId", 1), ("joinedAt", -1)]),
    _spec(JA, [("guildId", 1), ("joinedAt", -1)]),
    _spec(JA, [("guildId", 1), ("attributionStatus", 1), ("joinedAt", -1)]),
    _spec(JS, [("guildId", 1), ("userId", 1)], unique=True),
    _spec(JS, [("guildId", 1), ("joinedAt", -1)]),
    _spec(RS, [("guildId", 1), ("userId", 1)], unique=True),
    _spec(RS, [("guildId", 1), ("updatedAt", -1)]),
    _spec(RS, [("guildId", 1), ("lastSeenAt", -1)]),
    _spec(RS, [("guildId", 1), ("pendingRestore", 1), ("updatedAt", -1)]),
    _spec(NS, [("guildId", 1), ("userId", 1)], unique=True),
    _spec(NS, [("guildId", 1), ("updatedAt", -1)]),
    _spec(NS, [("guildId", 1), ("lastSeenAt", -1)]),
    _spec(NS, [("guildId", 1), ("pendingRestore", 1), ("updatedAt", -1)]),
    _spec(NH, [("guildId", 1), ("userId", 1), ("changedAt", -1)]),
    _spec(NH, [("guildId", 1), ("changedAt", -1)]),
    # --- stalker
    _spec(SS, [("guildId", 1), ("watcherUserId", 1)]),
    _spec(SS, [("guildId", 1), ("targetUserId", 1)]),
    _spec(SS, [("guildId", 1), ("watcherUserId", 1), ("targetUserId", 1)], unique=True),
    # --- chat
    _spec(CM, [("guildId", 1), ("messageId", 1)], unique=True, name="chat_guildId_messageId_unique"),
    _spec(CM, [("guildId", 1), ("channelId", 1), ("sentAt", -1)], owner="shared",
          name="chat_guildId_channelId_sentAt"),
    _spec(CM, [("guildId", 1), ("sentAt", -1)], name="chat_guildId_sentAt"),
    _spec(CP, [("guildId", 1), ("createdAt", 1)], owner="web", name="chat_presets_guildId_createdAt"),
    # --- audit (web-выборки; bot site_audit пишет в web_audit_logs)
    _spec(WA, [("guildId", 1), ("at", -1)], owner="web", name="web_audit_guildId_at"),
    _spec(DA, [("guildId", 1), ("at", -1)], owner="web", name="web_disc_audit_guildId_at"),
    _spec(DA, [("guildId", 1), ("entryId", 1)], owner="web", name="web_disc_audit_guildId_entryId"),
    # --- operations journal (T08)
    _spec(OP, [("guildId", 1), ("batchId", 1)], owner="web", name="web_operations_guildId_batchId"),
    _spec(OP, [("guildId", 1), ("createdAt", -1)], owner="web", name="web_operations_guildId_createdAt"),
    # --- только runner'ом (M2/M3): additive TTL и уникальный индекс поверх dedup
    _spec(OP, [("createdAt", 1)], owner="runner", name="operations_createdAt_ttl",
          ttl=OPERATIONS_TTL_SECONDS),
    _spec(DA, [("guildId", 1), ("entryId", 1)], owner="runner", unique=True,
          name="discord_audit_guildId_entryId_unique"),
    # --- T10.1: обнаружены read-only снимком прод-Mongo 2026-09-25 и НЕ объявлены ни в
    # bot, ни в web коде. Т10.7 запрещает молча удалять «выглядящие лишними» индексы:
    # они могут держать инвариант старых версий. owner=legacy — документированы,
    # не создаются, не проверяются на startup; решение о drop — отдельный управляемый шаг.
    _spec(P, [("guildId", 1), ("active", 1), ("userId", 1)], owner="legacy",
          name="web_guild_active_user"),
    _spec(P, [("guildId", 1), ("joinedAt", -1)], owner="legacy", name="web_guild_joinedAt"),
)

COLLECTIONS: tuple[str, ...] = tuple(dict.fromkeys(spec.collection for spec in MANIFEST))


def manifest_json() -> str:
    return json.dumps(
        {"schemaVersion": SCHEMA_VERSION, "indexes": [spec.spec_json() for spec in MANIFEST]},
        indent=2,
        sort_keys=True,
    )


def manifest_checksum() -> str:
    return hashlib.sha256(manifest_json().encode("utf-8")).hexdigest()


def specs_for(owners: tuple[str, ...]) -> tuple[IndexSpec, ...]:
    return tuple(spec for spec in MANIFEST if spec.owner in owners)


def spec_signature(spec: IndexSpec) -> tuple:
    """Хэшируемая каноническая форма спецификации (для сравнений в тестах)."""
    return (spec.collection, spec.key_str(), spec.unique, spec.sparse,
            _canon(spec.partial) if spec.partial is not None else "", spec.ttl, spec.name)


# ---------------------------------------------------------------- сверка (DB01/DB02)


class SchemaIncompatible(RuntimeError):
    """Индекс с теми же ключами, но другой спецификацией — молча не чинится (DB02)."""


def _flags_match(actual: dict, spec: IndexSpec) -> list[str]:
    diffs: list[str] = []
    if bool(actual.get("unique", False)) != spec.unique:
        diffs.append(f"unique: actual={bool(actual.get('unique', False))} expected={spec.unique}")
    if bool(actual.get("sparse", False)) != spec.sparse:
        diffs.append(f"sparse: actual={bool(actual.get('sparse', False))} expected={spec.sparse}")
    actual_partial = actual.get("partialFilterExpression")
    if _canon(actual_partial if actual_partial is not None else None) != (
        _canon(spec.partial) if spec.partial is not None else ""
    ):
        # Mongo может не возвращать partial для индексов без него — сравниваем только когда он есть у одного
        if not (actual_partial is None and spec.partial is None):
            diffs.append(f"partialFilterExpression: actual={_canon(actual_partial)} expected={_canon(spec.partial)}")
    actual_ttl = actual.get("expireAfterSeconds")
    if actual_ttl != spec.ttl:
        diffs.append(f"expireAfterSeconds: actual={actual_ttl} expected={spec.ttl}")
    return diffs


def actual_keys(index_doc: dict) -> tuple[tuple[str, int], ...]:
    return tuple((field, int(direction)) for field, direction in index_doc.get("key", {}).items())


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    matched: tuple[str, ...]  # "coll.index" — эквивалент найден (имя может отличаться)
    under_other_name: tuple[str, ...]  # DB01: принято под чужим именем
    missing: tuple[str, ...]
    incompatible: tuple[str, ...]

    def raise_if_incompatible(self) -> "VerifyReport":
        if self.incompatible:
            raise SchemaIncompatible(
                "нарушения контракта индексов (нужен план миграции, не слепой пересбор): "
                + "; ".join(self.incompatible)
            )
        return self


def _list_indexes(db: Any, coll: str) -> list[dict] | None:
    collection = db[coll]
    lister = getattr(collection, "list_indexes", None)
    if lister is None:  # unit-фейки: сверка недоступна, не имитируем
        return None
    try:
        return [dict(doc) for doc in lister()]
    except Exception as exc:  # коллекции нет в БД — считаем «индексов нет»
        if "NamespaceNotFound" in exc.__class__.__name__ or "ns not found" in str(exc).lower():
            return []
        raise


def _compare_grouped(by_coll, specs, matched, under_other_name, missing, incompatible) -> VerifyReport:
    for spec in specs:
        candidates = by_coll.get(spec.collection, [])
        same_keys = [doc for doc in candidates if actual_keys(doc) == spec.keys]
        exact = [doc for doc in same_keys if not _flags_match(doc, spec)]
        if exact:
            matched.append(f"{spec.collection}.{spec.name}")
            if exact[0].get("name") != spec.name:
                under_other_name.append(f"{spec.collection}.{spec.name}~({exact[0].get('name')})")
            continue
        if same_keys:
            diffs = _flags_match(same_keys[0], spec)
            incompatible.append(
                f"{spec.collection}[{spec.key_str()}] name={same_keys[0].get('name')}: " + ", ".join(diffs)
            )
            continue
        missing.append(f"{spec.collection}.{spec.name}")
    return VerifyReport(
        ok=not (missing or incompatible),
        matched=tuple(matched),
        under_other_name=tuple(under_other_name),
        missing=tuple(missing),
        incompatible=tuple(incompatible),
    )


def verify_db(db: Any, owners: tuple[str, ...] = ("bot", "web", "shared")) -> VerifyReport:
    """Read-only сверка фактической БД с манифестом (для указанных owners)."""
    specs = specs_for(tuple(sorted(owners)))
    by_coll: dict[str, list[dict]] = {}
    for spec in specs:
        if spec.collection in by_coll:
            continue
        docs = _list_indexes(db, spec.collection)
        if docs is None:  # фейк без list_indexes — сверка пропущена
            return VerifyReport(True, (), (), (), ())
        by_coll[spec.collection] = docs
    return _compare_grouped(by_coll, specs, [], [], [], [])


def verify_after_create(db: Any, report_created: Iterable[str]) -> VerifyReport:
    """Строгая для прода: несовместимость не скрывается (нарушения индексов видны)."""
    return verify_db(db)


def snapshot_indexes(db: Any, *, include_empty: bool = False) -> dict:
    """Read-only снимок индексов (без документов пользователей) — T10.1.
    Снимает ВСЕ коллекции БД (прод-наследие вне манифеста тоже видно)."""
    out: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "collections": {}}
    names = list(COLLECTIONS)
    lister = getattr(db, "list_collection_names", None)
    if lister is not None:
        try:
            names = sorted(set(lister()) | set(COLLECTIONS))
        except Exception:
            pass
    for name in names:
        docs = _list_indexes(db, name)
        if docs is None:
            continue
        entries = [
            {
                "name": doc.get("name"),
                "key": [list(item) for item in actual_keys(doc)],
                **({"unique": True} if doc.get("unique") else {}),
                **({"sparse": True} if doc.get("sparse") else {}),
                **({"partialFilterExpression": doc["partialFilterExpression"]}
                   if doc.get("partialFilterExpression") is not None else {}),
                **({"expireAfterSeconds": doc["expireAfterSeconds"]}
                   if doc.get("expireAfterSeconds") is not None else {}),
            }
            for doc in docs
        ]
        if not entries and not include_empty:
            continue
        out["collections"][name] = entries
    return out
