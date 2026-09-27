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
   `DurablePublisher` — обёртка publisher'ов сервисов; для `session.closed`/`session.summary.ready`
   event-id детерминирован из sessionId (`deterministic_event_id`) — дважды созданное событие
   для одной сессии = один документ (T09.2/E04). Для `voice.events` издатель под per-scope
   lock выдаёт строке монотонный `seq` (R26-02, см. 4a).
2. **Inbox** (`event_inbox`): состояние на `(eventId, consumer)` — received → processing →
   completed | quarantined. Lease 120 s с `leaseToken`; протухший lease забирается через CAS.
   `deliver()` — **единственная точка исполнения handler'а и для wire, и для sweep**
   (T09.10: нет второго пути, нет двойного эффекта).
3. **Повторы и яд** (T09.3/E03, .9/E08): ошибка handler'а → `fail_attempt`: attempts < max_deliver(8)
   → received (повторит sweep или повторная доставка), ≥ → quarantined с причиной.
   Битый конверт/подпись → `quarantine_poison` по sha256 от сырых байт (уникальный id, без каскада).
   Завершённое событие повторно `deliver()` → `skipped` (идемпотентно).
4. **Порядок и догрузка** (T09.5 / R26-01): sweep — **курсорный** (миграция M5):
   позиция `(createdAt,_id)` последней просмотренной строки хранится в
   `event_sweep_state` (ключ на `(consumer, subjects)`); forward-выборка идёт строго
   за курсором индексом `event_log (subject, createdAt, _id)` страницей ≤
   `EVENT_SWEEP_SCAN_LIMIT`; терминальные/активные inbox-строки пропускаются пакетно
   (без per-row find_one), курсор двигается только за ряды, покрытые inbox-строкой.
   Незавершённые за курсором добирает **retry-проход** по inbox
   (`received`/истёкший `processing`) — повторные попытки старых событий не
   исчезают за high-water mark; опоздавшие вставки с «протухшим» createdAt ловит
   **gap-проход** (окно `EVENT_SWEEP_GAP_SECONDS`, по умолчанию 120 s; 0 = выключен).
   Тик ограничен и по числу доставок (`limit`), и по числу DB-операций; весь
   синхронный Mongo I/O — вне event loop (`asyncio.to_thread`).
   Восстановление после простоя chronological. `deliver()` проверяет результат `complete()`:
   потерянный при takeover fence не отдаётся как completed (`fence_lost`).
4a. **Порядок live/догрузки (R26-02)**: для `voice.events` ключ порядка —
   scope `(guildId, userId)`; строки журнала несут `scope` и монотонный `seq`
   (счётчик `event_seq`, выдаётся `DurablePublisher` под per-scope asyncio.Lock
   в момент вставки в журнал — вставка и seq под одним локом, поэтому seq-порядок
   = порядку вставки в журнал внутри scope). Потребитель перед claim
   проверяет гейтом, что все предшественники того же (subject, scope) терминальны:
   незавершённых догоняет рекурсивно (окно `GATE_WINDOW=64` строк, `GATE_SCANS=3`
   прохода — длинный хвост добирает следующий retry-проход свипа, а не одна
   итерация), активный lease/карантин предшественника → событие встаёт в
   `deferred` без lease и без attempts (невинная жертва очереди не садится в
   карантин по `max_deliver`). Ватермарк `event_scope_progress (consumer,
   subject, scope).lastSeq` ограничивает повторные сканы завершённой историей;
   прогресс двигается только по терминальным предшественникам — хвост
   просроченного lease одного scope НЕ блокирует другие scope
   (head-of-line запрещён). Легаси-строки без seq доназначают его из того же
   счётчика по лексикографическому (orderAt,_id) порядку, где `orderAt` — время
   самого события (`occurredAt` из payload, при отсутствии — `createdAt`): BSON
   датируется с точностью до миллисекунды, и два события одного scope, записанные
   в один ms, по `createdAt` неразличимы. Строки без `orderAt` (доапгрейдные)
   считаются предшественниками, пока их собственный scan не донасит поле, —
   рекурсия при этом строго сужается; чужие subjects гейт не трогает. Индекс —
   миграция M6 (`event_log (subject, scope, seq)`).
   Строгий порядок гарантируется **внутри scope при одном процессе-издателе**
   (bucket-lock живёт в памяти gateway); второй writer отказывается стартовать
   через E09-гард (см. «Ограничения развёртывания»). Переходный смешанный
   backlog (часть строк с seq, часть без) упорядочивается best-effort:
   доназначенный seq всегда больше уже выданных, инверсия возможна только между
   легаси-строкой и новой, и только на время одного дренажа.
5. **Конверт v1** (T09.7/E07): `bus.Envelope` получил поле `v` (schema). `decode_envelope`
   отвергает не-1 (`unsupported envelope schema`) и старше `max_age_seconds` (конфиг
   `EVENT_MAX_AGE_SECONDS`, по умолчанию 3600). HMAC и issuer-проверка сохранены.
6. **Легаси-совместимость** (T09.10): старый `deduper`-путь в `bus.subscribe` оставлен;
   new-caller'ы передают `consumer=`+`db=`. `processed_messages` больше не используется новыми
   потребителями (stalker/writer перешли на inbox), класс `_ServiceDeduper` сохранён для тестов/легаси.
7. **Наблюдаемость** (T09.6/E01): `pending_stats()` → processed/quarantined/backlog/oldest +
   `deferred` (R26-02: события, ждущие предшественников своего scope);
   каждый sweep-цикл (интервал `EVENT_SWEEP_INTERVAL_SECONDS`, по умолчанию 15 s) логирует их;
   writer дополняет 60-секундным reconcile (независимый корректор состояния).
   Событие, которое потребители ещё не обработали, **не** считается потерянным: wire-доставка —
   быстрый путь, durability даёт журнал.

## Граница гарантий (T09.8 / E10 — честно)
- Гарантируется: событие, **принятое журналом** (`record()` вернул id), будет доставлено каждому
  зарегистрированному потребителю ≥1 раз при наличии хотя бы одного живого цикла sweep
  (wire — быстрый путь, durability даёт журнал); повтор доставки не повторяет эффект
  (inbox CAS + `skipped` на completed). `record()` падает исключением — публикация
  считается не состоявшейся, тишина невозможна (издатель не получит «успех» при
  незаписанном событии). Исключение in-process подписчика на `record()` не влияет:
  outbox гарантирует доставку **потребителям через журнал**, а не внутрипроцессные колбэки.
- Порядок (R26-02): полный порядок `voice.events` **внутри scope (guild,user)** между
  live/wire, догрузкой из журнала и replay — обеспечивает гейт доставки (см. 4a), а не
  «один подписчик на subject». Условие издателя: **одна реплика gateway на ordering scope
  + детерминированный ключ + запись в журнал до публикации**. Second writer = отказ старта
  (E09-гард). Между разными scope порядок не определяется и не нужен.
- **Не** гарантируется: доставка события, которое не было записано (падение продюсера до
  `record()`), и строгий глобальный порядок между процессами/replicas. Детекция пробела —
  reconcile-writer и периодические сверки, а не выдумывание событий: ненаблюдённые события
  не восстанавливаются.
- JetStream остаётся заменой «как есть», когда `dashboard-nats` получит volume + постоянный
  storage (инфра-задача вне T09); API `eventlog` тогда тонко ложится на JS-стримы.

### Инвентарь эффектов потребителей (R26-02.4) — inbox completed подтверждает, но не заменяет идемпотентность
| consumer | эффект | стратегия при повторе | граница |
|---|---|---|---|
| tracker (`voice.events`) | DB: сессии/участники | get-or-create по уникальному partial-индексу + presence-проверка — повтор no-op | тесты: крах после эффекта до completed, перехват lease |
| writer (`session.closed`) | DB: закрытие сессии + summary + `closedEventPublishedAt` | переход по состоянию сессии: повторная доставка закрытой сессии — no-op | окно send-marker см. reconciliation 60 s |
| gateway (`session.summary.ready`) | Discord: правка карточки | edit по содержимому идемпотентен | — |
| activity (`activity.events`) | Discord: пост embed во внешний канал | **не идемпотентен**: крах между `channel.send` и `complete()` повторяет пост (at-least-once окно) | принято осознанно: Discord не даёт dedup-ключа для channel.send; дедуп требовал бы journal исходящих постов (вне объёма) |
| stalker (`voice.events`/`activity.events`) | Discord: DM наблюдателям | тот же класс: повторенный sweep может повторно прислать DM; пересылка одному watch'er'у не блокирует остальных | та же граница at-least-once |

## Ограничения развёртывания (E09)
- `gateway` — единственный writer порядка `voice.events`: **1 replica**, плюс startup-гард
  (`supervise.claim_single_writer`): перед подключением к NATS/Discord сервис читает
  `bot_runtime_heartbeats(worker=gateway)` и отказывается стартовать (`SystemExit`), если
  СВЕЖИЙ heartbeat (окно `GATEWAY_SINGLETON_MAX_AGE_SECONDS`, по умолчанию 90 s = 6 тиков)
  принадлежит другому `instance` (hostname контейнера). Рестарт того же контейнера
  (тот же instance) и старт после graceful stop (флаг `stopped`, снимается в `finally`
  main) не блокируются. Окно=0 выключает гард (явное решение оператора). После аварийной
  смерти старого процесса (release не выполнен) новый container-id отвергается до истечения
  окна, затем проходит — рестарт-политика compose пересоздаёт старт, ожидание ≤ 90 s.
  Ограничение честно: гонка двух **холодных** стартов не атомарна (нет unique-индекса на
  worker в `bot_runtime_heartbeats`), а гард ловит штатный случай «старый живой + новый».
- `tracker` — ровно **1 replica** (stack.yaml): два трекера создавали бы параллельные очереди
  на один consumer-имя. Остальные потребители могут масштабироваться (inbox делит работу
  через lease-CAS; гейт порядка — per-consumer, эффект от двух реплик одного consumer'а
  идемпотентен по inbox), но эффект `session.closed` идемпотентен только при одном writer —
  writer тоже 1 replica.

## Последствия
- `event_log`/`event_inbox` растут без TTL (рост = число событий × потребителей, не повторов);
  ротация/архив — T10 (там же индексы миграционным раннером).
- Боевая миграция (T18): перед переключением оба инбокса пусты, wire и sweep включают
  одновременно через один `deliver()`; откат — выключить sweep-таски, legacy-deduper путь цел.
- Конфиг: `EVENT_MAX_AGE_SECONDS`, `EVENT_SWEEP_INTERVAL_SECONDS`, `EVENT_MAX_DELIVER`,
  `EVENT_SWEEP_GAP_SECONDS`, `EVENT_SWEEP_SCAN_LIMIT`, `GATEWAY_SINGLETON_MAX_AGE_SECONDS`
  (runtime.load_config, все положительны, иначе fallback; GAP и SINGLETON допускают 0 = выключен).
- Миграция M6 создаёт индекс `event_log (subject, scope, seq)` — **применять до нового образа**
  (гейт без индекса верен, но дорог на большом журнале).
