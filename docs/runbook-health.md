# Runbook: liveness, readiness и рестарты (T12)

## Контракты

| Сигнал | Где | Что означает | Чего НЕ означает |
| --- | --- | --- | --- |
| liveness web | `GET /api/healthz` (всегда 200, пока процесс обслуживает запросы) | event loop жив, HTTP отвечает | пригодность выполнять работу |
| readiness web | `GET /api/readyz` (200/503) | Mongo пингуется в пределах timeout, схема ok, media mount на месте (если настроен), критичные фоновые циклы не умерли nasмерть | доступность Discord (внешний отказ — отдельный статус в `/api/audit/discord/status`) |
| readiness бота | документ `bot_runtime_heartbeats {worker}` + контейнерный `HEALTHCHECK` (`python -m voice_tracker.healthcheck`) | свежий heartbeat этого worker-а + health contract воркера: required loops живы (running), без серии отказов (consecutiveFailures == 0), у critical loops последний успешный тик (lastTickAt) не старше max_progress_age, required deps (NATS/Discord) в правильном состоянии | наличие пользовательских событий (heartbeat — по таймеру, R03); «контейнер healthy» ≠ «другой сервис healthy» (см. про T17 ниже) |

## Что именно проверяет readiness бота (R26-10)

- **Свежесть heartbeat** — `updated_at` дока не старше `--max-age` (default 90s;
  цикл пишет каждые ~15s). Просроченный heartbeat коротко замыгает контракт:
  дальше проверять нечего, процесс не даёт признаков жизни.
- **Per-service health contract** (`voice_tracker/healthcheck.py`, worker →
  loops/deps): каждый обязательный критичный цикл сервиса (`*-event-sweep`,
  `gateway-managed-voice-reconcile`, `gateway-voice-session-reaper`, …) должен
  быть `running`, `consecutiveFailures == 0` и с `lastTickAt` не старше
  договора по его реальному интервалу (sweep 15–60s → лимит 180s, reconcile 5s
  → 90s, reaper 90+120s → 300s, heartbeat-цикл → 60s). Запас ≥ 3× интервала —
  штатно длинная итерация не даёт false positive.
- **Progress beats (R26-10.3):** production-циклы отмечают `supervisor.beat()`
  только полностью успешную итерацию и `supervisor.fail(name, exc)` — любую
  проглоченную внутри `while` ошибку. Залипший/деградивший цикл виден по
  сериям `supervise event=iteration_failed ... task=... consecutive=N`.
- **Non-critical loops** (`gateway-invite-*`, `gateway-member-role-reconcile`)
  входят в снапшот (наблюдаемы: failures/ошибки в heartbeat и логах), но
  readiness НЕ снимают — их починку обеспечивает supervisor-respawn.
- **Startup grace:** в первые `startup_grace_seconds` (120s от `started_at`
  дока) допускается ещё не набитый `lastTickAt` — цикл стартовал, период тика
  не истёк. После grace отсутствие тика = unhealthy (fail-closed).
- **Fail-closed:** unknown/missing/битые поля контракта или снапшота,
  неизвестная identity (`SERVICE_NAME` > `SERVICE` > `tracker`; Dockerfile
  запекает `SERVICE`) — unhealthy/exit 1, а не «почти здоров».

## Как health связан с рестартом (п.7 плана)

1. `docker compose` в этом репозитории задаёт `restart: unless-stopped` —
   падающий/убивающий себя процесс перезапускается политикой.
2. `HEALTHCHECK` контейнера **сам по себе ничего не рестартит** (в чистом
   Docker unhealthy-статус — только метка). Рестарт по unhealthy потребовал бы
   отдельного автохилера — он сознательно НЕ подключён: устойчивая ошибка
   конфигурации/зависимости при авторестартах превращается в restart storm.
3. Приложения внутри процесса не долбят упавшую зависимость: supervisor
   перезапускает фоновые циклы с экспоненциальным backoff и полным джиттером
   (`voice_tracker/supervise.py`), старт writer'а — тоже с джиттером.
4. Возврат зависимости возвращает readiness без ручных действий: web-ping и
   schema-recheck считаются заново на каждом запросе/в фоне (R07); heartbeat
   бота продолжает обновляться, и контейнер сам становится healthy.

## Диагностика и безопасная «рекувери» (R26-10)

### Единственный gateway-писатель

Новый gateway захватывает `bot_single_writer_leases` по `_id="gateway"`.
Mongo гарантирует уникальность `_id`; каждый захват выдаёт новый `owner` и
увеличивает `fence`. Продление и снятие требуют совпадения обоих значений.
Heartbeat в `bot_runtime_heartbeats` служит диагностикой и не выдаёт право
публикации. При потере lease gateway прекращает публикацию и закрывает Discord
соединение. Для просмотра без записи: `db.bot_single_writer_leases.findOne({_id:"gateway"})`.

**Переход со старого образа:** остановить старый `dsbot-gateway` и убедиться,
что он завершился, затем запустить образ с новым lease. Старый образ знает лишь
heartbeat и не участвует в новом CAS; одновременный rolling-переход двух версий
не даёт гарантии одного писателя. После перехода запускать ровно одну реплику;
отдельным тестом проверить отказ второго экземпляра и перехват после остановки.

- Структурные строки лога: `supervise event=task_failed task=... consecutive=N
  error=<ТипИсключения>` (цикл погиб и перезапущен) и
  `supervise event=iteration_failed task=... consecutive=N error=<Тип>` (цикл
  жив, итерация провалена) — имя типа, без сообщений (R06).
- `db["bot_runtime_heartbeats"].find({})` — кто жив, снапшоты петель (restarts,
  consecutiveFailures, lastErrorType, lastTickAt, running), NATS
  `connected/reconnecting`, Discord `latencyMs`.
- Отставание журнала событий: строки `... event sweep delivered=... backlog=...
  oldestPendingAgeSeconds=... quarantined=...` в логах writer/tracker/activity.
- Handcheck одного сервиса изнутри контейнера:
  `docker compose exec writer python -m voice_tracker.healthcheck --service writer`
  — читает только Mongo, ничего не перезапускает и не пишет (readonly-диагностика).
- **Unhealthy сам ничего не чинит и не рестартит.** Первый шаг — diagnosis из
  логов/heartbeat-дока, а не `docker restart`: авторестарт при устойчивом
  отказе зависимости превращается в restart storm. Рестарт оправдан только
  когда диагноз — «процесс залип и не даёт heartbeat», и выполняется штатным
  механизмом (restart policy / redeploy тега), последовательно, по одному
  сервису.

## Честная оговорка про T17-стенд и fake-контейнер (R26-10.6)

В изолированном e2e-стенде T17 (`docker-compose.t17.yml`) сервис `fake`
запускает **тот же бот-образ**, что и tracker/writer, но другую команду
(`python /h/fake_discord.py` — подделка Discord-API). Образ несёт в себе
`HEALTHCHECK` из Dockerfile (`python -m voice_tracker.healthcheck`), поэтому
контейнер `fake` крутит healthcheck **бота**, который по precedence
`--service > SERVICE_NAME > SERVICE > tracker` резолвит identity по env образа,
а **не** по тому, какой процесс реально внутри запущен. Отсюда два правила:

1. **Health контейнера `fake` не является доказательством health реального
   tracker-а** (и наоборот): healthcheck `fake` читает heartbeat-док чужого
   воркера из той же БД и может быть healthy, пока сам fake-процесс мёртв, или
   unhealthy из-за чужого воркера. Статус healthcheck привязан к worker-identity
   в команде healthcheck, а не к процессу контейнера.
2. **Различать container- и service-identity** при разборе стенда: сверять
   (а) имя контейнера/сервиса в compose, (б) фактическую `command:` контейнера,
   (в) какой worker читает его HEALTHCHECK (`docker inspect` команды +
   `SERVICE`/`SERVICE_NAME` в env образа), и (г) `worker`/`instance` в
   heartbeat-доке, на который ссылается статус. Доказательство health сервиса —
   только его собственный heartbeat-док `{worker}` c loops/dep-снапшотом,
   полученный явным `--service <name>`; зелёный `Status: healthy` у контейнера
   чужого назначения ничего о сервисе не говорит.

Корень — в конфиге стенда (внешний относительно бот-репо проект), а не в коде
бота: fake-сервису в стенде следует отключать/переопределять healthcheck
(`healthcheck: disable: true`) либо задавать ему собственную неразнённую
identity. Из репозитория бота это не чинится и не маскируется: здесь health —
строго про worker-док в Mongo.

## Что выбрано как «один минимальный мониторинг» (п.6 плана)

Health/heartbeat + логи с структурными полями. Внешний alerting-канал не
подключён: он требует выбора владельца (решение владельца продукта — см.
AI_RELEASE_DECISIONS.md), и без этого решения ничего стороннего не заводится.
