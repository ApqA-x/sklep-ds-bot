# ADR-0002: Надежные события — Mongo outbox/inbox поверх Core NATS (T09)

## Статус
Принят, реализован в `voice_tracker/eventlog.py` (журнал), `voice_tracker/bus.py` (конверт v1),
потребители: tracker / activity / stalker / writer / gateway.

## Контекст
Plan T09 требует: устойчивое принятие событий, порядок, повторную доставку, изоляцию яда,
без двойных эффектов. Штатный вариант — JetStream, но боевой `dashboard-nats` поднят
**без persistent volume**: JetStream-файлы переживали бы только рестарт контейнера, не
потерю диска/ноды. Гарантии «NATS сохранит» было бы документальной ложью.

## Решение
Единственный устойчивый носитель в текущем контуре — Mongo (общий для всех сервисов),
поэтому: **outbox-журнал + per-consumer inbox на стороне приложений**, транспорт — Core NATS.

### Каталог subject → producer → consumers → effect → repair (T09.1)
| subject | producer | consumers | эффект потребителя | ремонт |
|---|---|---|---|---|
| `voice.events` | voice-gateway (`gateway.py`, внешний источник голосовых состояний) | tracker, stalker | запись голосовой активности | wire + sweep(consumer=tracker/stalker) |
| `activity.events` | gateway (`publish_activity_event`, outbox) | activity, stalker | запись активности | wire + sweep |
| `session.closed` | tracker (summary-pipeline) и gateway-reaper (outbox) | writer | закрытие сессии, генерация summary | republish_pending + sweep(writer); 60 s service-reconcile |
| `session.summary.ready` | writer (outbox) | gateway (рассылка Discord) | редактирование карточки | republish_pending + sweep(gateway) |

### Механика
1. **Outbox** (`event_log`): `publish_durable()` = `record()` → `bus.publish_json(message_id=event_id)`.
   `publishedAt=None` + `publishError` при сбое транспорта → `republish_pending()` повторят
   **с тем же message_id** (T09.4: идентичность события не меняется при репаблише).
   `DurablePublisher` — обёрткаpublisher'ов сервисов; для `session.closed`/`session.summary.ready`
   event-id детерминирован из sessionId (`deterministic_event_id`) — дважды созданное событие
   для одной сессии = один документ (T09.2/E04).
2. **Inbox** (`event_inbox`): состояние на `(eventId, consumer)` — received → processing →
   completed | quarantined. Lease 120 s с `leaseToken`; протухший lease забирается через CAS.
   `deliver()` — **единственная точка исполнения handler'а и для wire, и для sweep**
   (T09.10: нет второго пути, нет двойного эффекта).
3. **Повторы и яд** (T09.3/E03, .9/E08): ошибка handler'а → `fail_attempt`: attempts < max_deliver(8)
   → received (повторит sweep или повторная доставка), ≥ → quarantined с причиной.
   Битый конверт/подпись → `quarantine_poison` по sha256 от сырых байт (уникальный id, без каскада).
   Завершённое событие повторно `deliver()` → `skipped` (идемпотентно).
4. **Порядок** (T09.5): sweep идёт по `createdAt` (индекс `event_log (subject, createdAt)`;
   состояние потребителя — индекс `event_inbox (consumer, state)`), т.е.
   восстановление после простоя chronological; live-порядок внутри одного потребителя обеспечивает
   один подписчик на subject + однопоточный обработчик. Cross-process порядок не гарантируется —
   см. границу гарантий.
5. **Конверт v1** (T09.7/E07): `bus.Envelope` получил поле `v` (schema). `decode_envelope`
   отвергает не-1 (`unsupported envelope schema`) и старше `max_age_seconds` (конфиг
   `EVENT_MAX_AGE_SECONDS`, по умолчанию 3600). HMAC и issuer-проверка сохранены.
6. **Легаси-совместимость** (T09.10): старый `deduper`-путь в `bus.subscribe` оставлен;
   new-caller'ы передают `consumer=`+`db=`. `processed_messages` больше не используется новыми
   потребителями (stalker/writer перешли на inbox), класс `_ServiceDeduper` сохранён для тестов/легаси.
7. **Наблюдаемость** (T09.6/E01): `pending_stats()` → processed/quarantined/backlog/oldest;
   каждый sweep-цикл (интервал `EVENT_SWEEP_INTERVAL_SECONDS`, по умолчанию 15 s) логирует их;
   writer дополняет 60-секундным reconcile (независимый корректор состояния).
   Событие, которое потребители ещё не обработали, **не** считается потерянным: wire-доставка —
   быстрый путь, durability даёт журнал.

## Граница гарантий (T09.8 / E10 — честно)
- Гарантируется: событие, **принятое журналом** (record в Mongo), будет доставлено каждому
  зарегистрированному потребителю ≥1 раз при наличии хотя бы одного живого цикла sweep;
  повтор доставки не повторяет эффект (inbox CAS).
- **Не** гарантируется: доставка события, которое не было записано (падение продюсера до
  `record()`), и строгий глобальный порядок между процессами/replicas. Детекция пробела —
  reconcile-writer и периодические сверки, а не выдумывание событий: ненаблюдённые события
  не восстанавливаются.
- JetStream остаётся заменой «как есть», когда `dashboard-nats` получит volume + постоянный
  storage (инфра-задача вне T09); API `eventlog` тогда тонко ложится на JS-стримы.

## Ограничения развёртывания (E09)
`tracker` — ровно **1 replica** (stack.yaml): два трекера создавали бы параллельные очереди
на один consumer-имя, live-порядок `voice.events` перестал бы существовать. Остальные
потребители могут масштабироваться (inbox делит работу через lease-CAS), но эффект
`session.closed` идемпотентен только при одном writer — writer тоже 1 replica.

## Последствия
- `event_log`/`event_inbox` растут без TTL (рост = число событий × потребителей, не повторов);
  ротация/архив — T10 (там же индексы миграционным раннером).
- Боевая миграция (T18): перед переключением оба инбокса пусты, wire и sweep включают
  одновременно через один `deliver()`; откат — выключить sweep-таски, legacy-deduper путь цел.
- Конфиг: `EVENT_MAX_AGE_SECONDS`, `EVENT_SWEEP_INTERVAL_SECONDS`, `EVENT_MAX_DELIVER`
  (runtime.load_config, все положительны, иначе fallback).
