"""R26-02 regression: порядок live/догрузки и идемпотентность эффектов (V26-05).

Смысловые сценарии ревью 26.09 (здесь ожидается ИСПРАВЛЕННОЕ поведение):
  - пропущенный JOIN в журнале + live LEAVE: wire-доставка LEAVE обязана
    догнать JOIN того же scope и примениться ПОСЛЕ него — фантомной
    открытой сессии оставаться не должно;
  - эффект применён, а completed не записан (крах/перехват lease): повтор не
    удваивает эффекты (одна сессия, один participant, один events.closed);
  - задержка одного scope не блокирует другой (head-of-line запрещён):
    заблокированное событие живёт в deferred без attempts и lease;
  - карантин предшественника останавливает свой scope, события остальных
    участников доставляются;
  - издатель выдаёт монотонный seq внутри scope под bucket-локом; повторная
    публикация того же события seq не меняет.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402
from voice_tracker import domain, eventlog  # noqa: E402
from voice_tracker.repository import Repository  # noqa: E402
from voice_tracker.tracker import Defaults, Service, decode_voice_event  # noqa: E402

pytestmark = pytest.mark.integration

TEST_MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")


def _server_up() -> bool:
    try:
        client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    guard_mongo_uri(TEST_MONGO_URI)
    if not _server_up():
        pytest.skip("test mongod is not running on %s" % TEST_MONGO_URI)
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=3000)
    name = f"voice_tracker_t26r02_{uuid.uuid4().hex[:10]}"
    guard_db_name(name)
    database = client[name]
    yield database
    client.drop_database(name)
    client.close()


class RecordingPublisher:
    def __init__(self) -> None:
        self.closed: list[dict] = []

    async def publish_json(self, subject, value):
        from voice_tracker.domain import to_jsonable

        self.closed.append({"subject": subject, "value": to_jsonable(value)})


def _voice(guild: str, user: str, *, channel: str, prev: str, at: datetime) -> dict:
    return {
        "guildId": guild,
        "userId": user,
        "userName": "synthetic",
        "channelId": channel,
        "previousChannelId": prev,
        "occurredAt": at.isoformat(),
        "isBot": False,
    }


def _tracker(db):
    repo = Repository(db)
    publisher = RecordingPublisher()
    service = Service(repo, publisher, Defaults(tracking_mode=domain.GUILD_TRACKING_MODE_ALL))

    async def voice(raw: bytes) -> None:
        await service.HandleVoiceEvent(decode_voice_event(raw))

    return repo, service, publisher, voice


async def test_wire_leave_waits_for_backlog_join_no_ghost(db) -> None:
    """Ровно сценарий прогона ревью (V26-05): JOIN только в журнале, live LEAVE
    приходит раньше догрузки. Гейт обязан сначала доставить JOIN, затем LEAVE;
    итог — закрытая сессия, фантома нет."""
    repo, _, publisher, voice = _tracker(db)
    base = datetime.now(UTC)
    join = _voice("100000", "200000", channel="300000", prev="", at=base)
    leave = _voice("100000", "200000", channel="", prev="300000", at=base + timedelta(minutes=1))
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, join, event_id="order-join")
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, leave, event_id="order-leave")

    outcome = await eventlog.deliver(
        db, "order-probe", "order-leave", domain.SUBJECT_VOICE_EVENT,
        json.dumps(leave).encode(), voice,
    )
    # гейт догнал JOIN и отпустил LEAVE в этой же доставке либо отложил;
    # в обоих случаях к моменту применения LEAVE сессия уже открыта JOIN'ом
    assert outcome in ("completed", "deferred")
    assert db.event_inbox.find_one({"eventId": "order-join", "consumer": "order-probe"})["state"] == "completed"
    if outcome == "deferred":
        drained = await eventlog.sweep_pending(db, "order-probe", [domain.SUBJECT_VOICE_EVENT], voice)
        assert drained >= 1

    assert repo.FindActiveSession("100000", "300000") is None
    session = db.voice_sessions.find_one({"guildId": "100000", "channelId": "300000"})
    assert session is not None and session["status"] == domain.SESSION_STATUS_CLOSED
    parts = list(db.voice_session_participants.find({"guildId": "100000", "userId": "200000"}))
    assert len(parts) == 1 and parts[0]["active"] is False
    # ровно один events.closed: JOIN был применён до LEAVE, повторных закрытий нет
    closes = [c for c in publisher.closed if c["subject"] == domain.SUBJECT_SESSION_CLOSED]
    assert len(closes) == 1


async def test_join_effect_retry_does_not_duplicate(db) -> None:
    """Эффект применён, completed не записан (handler упал после эффекта —
    «крах»): retry повторяет handler, эффекты идемпотентны — одна сессия,
    один participant; затем LEAVE закрывает."""
    repo, _, _, voice = _tracker(db)
    base = datetime.now(UTC)
    join = _voice("100000", "200001", channel="300010", prev="", at=base)
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, join, event_id="dup-join")

    calls = {"n": 0}

    async def flaky(raw: bytes) -> None:
        await voice(raw)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash after effect, before completed")

    outcome = await eventlog.deliver(
        db, "dup", "dup-join", domain.SUBJECT_VOICE_EVENT, json.dumps(join).encode(), flaky)
    assert outcome == "received"  # E03: освобождён lease для retry
    row = db.event_inbox.find_one({"eventId": "dup-join", "consumer": "dup"})
    assert row["state"] == "received" and row["attempts"] == 1

    delivered = await eventlog.sweep_pending(db, "dup", [domain.SUBJECT_VOICE_EVENT], voice)
    assert delivered == 1
    assert db.event_inbox.find_one({"eventId": "dup-join", "consumer": "dup"})["state"] == "completed"
    sessions = list(db.voice_sessions.find({"guildId": "100000", "channelId": "300010"}))
    assert len(sessions) == 1 and sessions[0]["status"] == "active"
    parts = list(db.voice_session_participants.find({"userId": "200001"}))
    assert len(parts) == 1 and parts[0]["active"] is True

    # повторная доставка completed-события — skipped, эффектов ноль
    again = await eventlog.deliver(
        db, "dup", "dup-join", domain.SUBJECT_VOICE_EVENT, json.dumps(join).encode(), voice)
    assert again == "skipped"
    assert len(list(db.voice_session_participants.find({"userId": "200001"}))) == 1


async def test_lease_takeover_replay_and_double_close_once(db) -> None:
    """Истёкший lease перехватывается, повтор LEAVE поверх закрытой сессии не
    публикует второе events.closed и не кроит историю заново."""
    repo, _, publisher, voice = _tracker(db)
    base = datetime.now(UTC)
    join = _voice("100000", "200002", channel="300020", prev="", at=base)
    leave = _voice("100000", "200002", channel="", prev="300020", at=base + timedelta(minutes=5))
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, join, event_id="tk-join")
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, leave, event_id="tk-leave")
    await eventlog.deliver(db, "tk", "tk-join", domain.SUBJECT_VOICE_EVENT, json.dumps(join).encode(), voice)

    # «крах после эффекта до completed»: inbox остаётся processing с просроченным lease
    iid = eventlog.inbox_id("tk-leave", "tk")
    claimed = eventlog.claim(db, "tk-leave", "tk", domain.SUBJECT_VOICE_EVENT)
    assert claimed is not None
    await voice(json.dumps(leave).encode())
    db.event_inbox.update_one({"_id": iid}, {"$set": {"leaseExpiresAt": datetime.now(UTC) - timedelta(seconds=1)}})

    outcome = await eventlog.deliver(db, "tk", "tk-leave", domain.SUBJECT_VOICE_EVENT, json.dumps(leave).encode(), voice)
    assert outcome in ("completed", "fence_lost", "deferred", "skipped")
    await eventlog.sweep_pending(db, "tk", [domain.SUBJECT_VOICE_EVENT], voice)
    assert repo.FindActiveSession("100000", "300020") is None
    closes = [c for c in publisher.closed if c["subject"] == domain.SUBJECT_SESSION_CLOSED]
    assert len(closes) == 1
    # второй прогон того же LEAVE — skipped
    again = await eventlog.deliver(db, "tk", "tk-leave", domain.SUBJECT_VOICE_EVENT, json.dumps(leave).encode(), voice)
    assert again == "skipped"


async def test_slow_scope_does_not_block_others_head_of_line(db) -> None:
    """Актиный lease JOIN участника A откладывает LEAVE A в deferred — но
    события участника B (другой scope) доставиваются полностью; deferred не
    ест attempts и виден в статистике; после завершения A свип догоняет LEAVE A."""
    repo, _, _, voice = _tracker(db)
    base = datetime.now(UTC)
    a_join = _voice("100000", "200003", channel="300030", prev="", at=base)
    a_leave = _voice("100000", "200003", channel="", prev="300030", at=base + timedelta(minutes=1))
    b_join = _voice("100000", "200004", channel="300030", prev="", at=base)
    b_leave = _voice("100000", "200004", channel="", prev="300030", at=base + timedelta(minutes=1))
    for eid, ev in (("a-join", a_join), ("a-leave", a_leave), ("b-join", b_join), ("b-leave", b_leave)):
        eventlog.record(db, domain.SUBJECT_VOICE_EVENT, ev, event_id=eid)

    held = eventlog.claim(db, "a-join", "hul", domain.SUBJECT_VOICE_EVENT)
    assert held is not None
    await voice(json.dumps(a_join).encode())  # эффект JOIN есть, completed нет

    outcome = await eventlog.deliver(db, "hul", "a-leave", domain.SUBJECT_VOICE_EVENT, json.dumps(a_leave).encode(), voice)
    assert outcome == "deferred"
    row = db.event_inbox.find_one(
        {"_id": eventlog.inbox_id("a-leave", "hul")})
    assert row["state"] == "deferred" and row["attempts"] == 0 and row["leaseExpiresAt"] is None

    # B не ждёт A: оба события доставлены здесь же
    assert await eventlog.deliver(db, "hul", "b-join", domain.SUBJECT_VOICE_EVENT, json.dumps(b_join).encode(), voice) == "completed"
    assert await eventlog.deliver(db, "hul", "b-leave", domain.SUBJECT_VOICE_EVENT, json.dumps(b_leave).encode(), voice) == "completed"
    assert repo.FindActiveSession("100000", "300030") is not None  # сессия A ещё открыта (JOIN держат)

    stats = eventlog.pending_stats(db, "hul", [domain.SUBJECT_VOICE_EVENT])
    assert stats["deferred"] == 1 and stats["backlog"] >= 1

    # отпускаем A → следующий тик догружает LEAVE A
    eventlog.complete(db, held)
    await eventlog.sweep_pending(db, "hul", [domain.SUBJECT_VOICE_EVENT], voice)
    assert db.event_inbox.find_one({"eventId": "a-leave", "consumer": "hul"})["state"] == "completed"
    parts = list(db.voice_session_participants.find({"userId": "200003"}))
    assert len(parts) == 1 and parts[0]["active"] is False


async def test_quarantined_predecessor_stops_own_scope_only(db) -> None:
    """Ядовитый JOIN уходит в карантин и останавливает свой scope (LEAVE ждёт
    в deferred, видно в статистике); другой scope работает; после ручной
    починки предшественника цепочка разблокируется."""
    repo, _, _, voice = _tracker(db)
    base = datetime.now(UTC)
    p_join = _voice("100000", "200005", channel="300050", prev="", at=base)
    p_leave = _voice("100000", "200005", channel="", prev="300050", at=base + timedelta(minutes=1))
    q_join = _voice("100000", "200006", channel="300050", prev="", at=base)
    q_leave = _voice("100000", "200006", channel="", prev="300050", at=base + timedelta(minutes=1))
    for eid, ev in (("p-join", p_join), ("p-leave", p_leave), ("q-join", q_join), ("q-leave", q_leave)):
        eventlog.record(db, domain.SUBJECT_VOICE_EVENT, ev, event_id=eid)

    fixed = {"yes": False}

    async def poison(raw: bytes) -> None:
        payload = json.loads(raw)
        if payload["userId"] == "200005" and not fixed["yes"]:
            raise RuntimeError("synthetic poison join")
        await voice(raw)

    # доводим p-join до карантина (max_deliver=1)
    await eventlog.deliver(db, "qt", "p-join", domain.SUBJECT_VOICE_EVENT, json.dumps(p_join).encode(), poison, max_deliver=1)
    assert db.event_inbox.find_one({"eventId": "p-join", "consumer": "qt"})["state"] == "quarantined"

    assert await eventlog.deliver(db, "qt", "p-leave", domain.SUBJECT_VOICE_EVENT, json.dumps(p_leave).encode(), poison) == "deferred"
    assert await eventlog.deliver(db, "qt", "q-join", domain.SUBJECT_VOICE_EVENT, json.dumps(q_join).encode(), poison) == "completed"
    assert await eventlog.deliver(db, "qt", "q-leave", domain.SUBJECT_VOICE_EVENT, json.dumps(q_leave).encode(), poison) == "completed"
    stats = eventlog.pending_stats(db, "qt", [domain.SUBJECT_VOICE_EVENT])
    assert stats["deferred"] == 1 and stats["quarantined"] == 1
    # карантиный предшественник НЕ посадил невинный LEAVE в карантин и не съел его attempts
    assert db.event_inbox.find_one({"eventId": "p-leave", "consumer": "qt"})["attempts"] == 0

    # ручная починка: яд убран, карантинный предшественник возвращён в received —
    # цепочка разблокируется свипом
    fixed["yes"] = True
    db.event_inbox.update_one(
        {"_id": eventlog.inbox_id("p-join", "qt")},
        {"$set": {"state": "received", "attempts": 0, "lastError": None}})
    await eventlog.sweep_pending(db, "qt", [domain.SUBJECT_VOICE_EVENT], voice)
    assert db.event_inbox.find_one({"eventId": "p-join", "consumer": "qt"})["state"] == "completed"
    assert db.event_inbox.find_one({"eventId": "p-leave", "consumer": "qt"})["state"] == "completed"
    assert repo.FindActiveSession("100000", "300050") is None


class FakeBus:
    def __init__(self, *, slow: float = 0.0) -> None:
        self.published: list[tuple[str, str]] = []
        self.slow = slow

    async def publish_json(self, subject, value, *, message_id=None):
        if self.slow:
            await asyncio.sleep(self.slow)
        self.published.append((subject, message_id or ""))


async def test_publisher_monotonic_seq_per_scope_and_republish_stable(db) -> None:
    """Издатель: под bucket-локом seq совпадает с порядком вставки журнала для
    каждого scope; конкурентные publish разных пользователей не смешивают
    порядки; повторная публикация того же события seq не меняет."""
    bus = FakeBus(slow=0.001)
    pub = eventlog.DurablePublisher(bus, db, issuer="test")
    base = datetime.now(UTC)
    join = _voice("100000", "200007", channel="300070", prev="", at=base)
    leave = _voice("100000", "200007", channel="", prev="300070", at=base + timedelta(minutes=1))
    other = _voice("100000", "200008", channel="300070", prev="", at=base)

    await asyncio.gather(
        pub.publish_json(domain.SUBJECT_VOICE_EVENT, join),
        pub.publish_json(domain.SUBJECT_VOICE_EVENT, other),
        pub.publish_json(domain.SUBJECT_VOICE_EVENT, leave),
    )
    scope_a = eventlog.derive_scope(domain.SUBJECT_VOICE_EVENT, join)
    rows_a = list(db.event_log.find({"scope": scope_a}).sort([("createdAt", 1), ("_id", 1)]))
    assert len(rows_a) == 2
    seqs_a = [r["seq"] for r in rows_a]
    assert all(isinstance(s, int) for s in seqs_a) and seqs_a == sorted(seqs_a)
    # первый по журналу — JOIN: вставка и выдача seq идут под одним локом scope
    assert rows_a[0]["payload"]["channelId"] == "300070"

    # повторная публикация того же event_id: seq не меняется
    eid = rows_a[0]["_id"]
    await pub._publish_ordered(domain.SUBJECT_VOICE_EVENT, join, eid)
    assert db.event_log.find_one({"_id": eid})["seq"] == seqs_a[0]

    # второй scope — независимый счётчик
    scope_b = eventlog.derive_scope(domain.SUBJECT_VOICE_EVENT, other)
    rows_b = list(db.event_log.find({"scope": scope_b}))
    assert len(rows_b) == 1 and isinstance(rows_b[0]["seq"], int)
    # все публикации дошли до транспорта (включая повтор)
    assert len(bus.published) == 4


async def test_unordered_subjects_pass_without_gate(db) -> None:
    """Строки без scope (чужие subjects) гейт не трогают: ретраи и порядок
    R26-01 для них не меняется."""
    seen: list[int] = []
    for i in range(5):
        eventlog.record(db, "audit.probe", {"i": i}, event_id=f"u-{i}")
    n = await eventlog.sweep_pending(db, "plain", ["audit.probe"], lambda raw: seen.append(json.loads(raw)["i"]))
    assert n == 5 and seen == [0, 1, 2, 3, 4]
    stats = eventlog.pending_stats(db, "plain", ["audit.probe"])
    assert stats["backlog"] == 0 and stats["deferred"] == 0


async def test_gate_watermark_bounds_predecessor_scans(db) -> None:
    """После полной отработки scope новые события не пересматривают завершённую
    историю: ватермарк event_scope_progress двинут и предшественники за ним не
    сканируются."""
    repo, _, _, voice = _tracker(db)
    base = datetime.now(UTC)
    for k in range(4):
        ev = _voice("100000", "200009", channel="300090", prev="", at=base + timedelta(seconds=k))
        eventlog.record(db, domain.SUBJECT_VOICE_EVENT, ev, event_id=f"w-{k}")
    await eventlog.sweep_pending(db, "wm", [domain.SUBJECT_VOICE_EVENT], voice)
    scope = eventlog.derive_scope(domain.SUBJECT_VOICE_EVENT, {"guildId": "100000", "userId": "200009"})
    key = eventlog._progress_id("wm", domain.SUBJECT_VOICE_EVENT, scope)
    doc = db.event_scope_progress.find_one({"_id": key})
    assert doc is not None and doc["lastSeq"] >= 4
    # следующие события того же scope стартуют с ватермарка
    late = _voice("100000", "200009", channel="300090", prev="", at=base + timedelta(seconds=9))
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, late, event_id="w-late")
    outcome = await eventlog.deliver(db, "wm", "w-late", domain.SUBJECT_VOICE_EVENT, json.dumps(late).encode(), voice)
    assert outcome == "completed"
    doc2 = db.event_scope_progress.find_one({"_id": key})
    assert doc2["lastSeq"] > doc["lastSeq"]


async def test_join_move_leave_wire_ahead_of_backlog_both_directions(db) -> None:
    """JOIN→MOVE→LEAVE (V26-05, расширение прогона): live-доставка среднего или
    последнего события поверх недогруженной истории не оставляет активного
    участника ни в одном канале. MOVE — событие с обоими каналами, scope тот
    же (guild,user), seq-порядок сохраняет его между JOIN и LEAVE."""
    repo, _, _, voice = _tracker(db)
    base = datetime.now(UTC)

    # (A) в журнале JOIN+MOVE, wire приносит MOVE: гейт обязан сначала
    # догрузить JOIN, эффект MOVE применён поверх открытой сессии
    a1 = _voice("100000", "200010", channel="300100", prev="", at=base)
    a2 = _voice("100000", "200010", channel="300101", prev="300100", at=base + timedelta(minutes=1))
    a3 = _voice("100000", "200010", channel="", prev="300101", at=base + timedelta(minutes=2))
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, a1, event_id="mv-a-join")
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, a2, event_id="mv-a-move")
    assert await eventlog.deliver(db, "mv-a", "mv-a-move", domain.SUBJECT_VOICE_EVENT, json.dumps(a2).encode(), voice) == "completed"
    assert db.event_inbox.find_one({"eventId": "mv-a-join", "consumer": "mv-a"})["state"] == "completed"
    # затем live LEAVE (в журнале тоже есть — издатель пишет до публикации)
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, a3, event_id="mv-a-leave")
    assert await eventlog.deliver(db, "mv-a", "mv-a-leave", domain.SUBJECT_VOICE_EVENT, json.dumps(a3).encode(), voice) == "completed"
    assert repo.FindActiveSession("100000", "300100") is None
    assert repo.FindActiveSession("100000", "300101") is None
    a_states = [p["active"] for p in db.voice_session_participants.find({"userId": "200010"})]
    assert len(a_states) == 2 and all(s is False for s in a_states)

    # (B) reverse: wire приносит LEAVE, вся предистория (JOIN+MOVE) только в
    # журнале — гейт догоняет цепочку до единой терминальной точки
    b1 = _voice("100000", "200011", channel="300102", prev="", at=base)
    b2 = _voice("100000", "200011", channel="300103", prev="300102", at=base + timedelta(minutes=1))
    b3 = _voice("100000", "200011", channel="", prev="300103", at=base + timedelta(minutes=2))
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, b1, event_id="mv-b-join")
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, b2, event_id="mv-b-move")
    eventlog.record(db, domain.SUBJECT_VOICE_EVENT, b3, event_id="mv-b-leave")
    outcome = await eventlog.deliver(db, "mv-b", "mv-b-leave", domain.SUBJECT_VOICE_EVENT, json.dumps(b3).encode(), voice)
    assert outcome == "completed"
    for eid in ("mv-b-join", "mv-b-move", "mv-b-leave"):
        assert db.event_inbox.find_one({"eventId": eid, "consumer": "mv-b"})["state"] == "completed", eid
    assert repo.FindActiveSession("100000", "300102") is None
    assert repo.FindActiveSession("100000", "300103") is None
    b_states = [p["active"] for p in db.voice_session_participants.find({"userId": "200011"})]
    assert len(b_states) == 2 and all(s is False for s in b_states)


class DeadBus:
    """Транспорт лежит: journal принимает, publish падает — восстановление по
    требованию очереди идёт из Mongo (republish_pending + sweep), не из NATS."""

    def __init__(self) -> None:
        self.calls = 0

    async def publish_json(self, subject, value, *, message_id=None):
        self.calls += 1
        raise ConnectionError("nats down")


async def test_transport_down_journal_is_recovery_source(db) -> None:
    """E02/E06: при недоступном Core NATS источник восстановления — журнал:
    seq выдан при записи, republish тем же id, sweep доставляет по порядку без
    фантомов после починки транспорта."""
    repo, _, _, voice = _tracker(db)
    base = datetime.now(UTC)
    join = _voice("100000", "200012", channel="300120", prev="", at=base)
    leave = _voice("100000", "200012", channel="", prev="300120", at=base + timedelta(minutes=1))
    dead = DeadBus()
    pub = eventlog.DurablePublisher(dead, db, issuer="gateway")
    eid_join = await pub.publish_json(domain.SUBJECT_VOICE_EVENT, join)
    eid_leave = await pub.publish_json(domain.SUBJECT_VOICE_EVENT, leave)
    row_join = db.event_log.find_one({"_id": eid_join})
    assert row_join["publishedAt"] is None and row_join["publishError"]
    assert row_join["seq"] == 1 and db.event_log.find_one({"_id": eid_leave})["seq"] == 2

    # транспорт починился — те же id переопубликованы (дедуп по message_id)
    bus = FakeBus()
    n = await eventlog.republish_pending(bus, db, domain.SUBJECT_VOICE_EVENT)
    assert n == 2
    assert sorted(m for _, m in bus.published) == sorted([eid_join, eid_leave])
    assert db.event_log.find_one({"_id": eid_join})["publishedAt"] is not None

    # потребитель ничего не получал по wire — восстановился только из журнала,
    # порядок JOIN→LEAVE сохранён гейтом, финал без фантомов
    delivered = await eventlog.sweep_pending(db, "rec", [domain.SUBJECT_VOICE_EVENT], voice)
    assert delivered == 2
    assert repo.FindActiveSession("100000", "300120") is None
    parts = list(db.voice_session_participants.find({"userId": "200012"}))
    assert len(parts) == 1 and parts[0]["active"] is False
