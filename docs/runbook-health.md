# Runbook: liveness, readiness и рестарты (T12)

## Контракты

| Сигнал | Где | Что означает | Чего НЕ означает |
| --- | --- | --- | --- |
| liveness web | `GET /api/healthz` (всегда 200, пока процесс обслуживает запросы) | event loop жив, HTTP отвечает | пригодность выполнять работу |
| readiness web | `GET /api/readyz` (200/503) | Mongo пингуется в пределах timeout, схема ok, media mount на месте (если настроен), критичные фоновые циклы не умерли nasмерть | доступность Discord (внешний отказ — отдельный статус в `/api/audit/discord/status`) |
| readiness бота | документ `bot_runtime_heartbeats {worker}` + контейнерный `HEALTHCHECK` (`python -m voice_tracker.healthcheck`) | loop сервиса жив и может писать в Mongo; в доке — снапшоты supervised-задач и состояние NATS/Discord | наличие пользовательских событий (heartbeat — по таймеру, R03) |

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

## Диагностика отказов

- Структурные строки лога: `supervise event=task_failed task=... consecutive=N
  error=<ТипИсключения>` — имя типа, без сообщений (R06).
- `db["bot_runtime_heartbeats"].find({})` — кто жив, снапшоты петель (restarts,
  consecutiveFailures, lastErrorType), NATS `connected/reconnecting`,
  Discord `latencyMs`.
- Отставание журнала событий: строки `... event sweep delivered=... backlog=...
  oldestPendingAgeSeconds=... quarantined=...` в логах writer/tracker/activity.
- Handcheck одного сервиса изнутри контейнера:
  `docker compose exec writer python -m voice_tracker.healthcheck --service writer`.

## Что выбрано как «один минимальный мониторинг» (п.6 плана)

Health/heartbeat + логи с структурными полями. Внешний alerting-канал не
подключён: он требует выбора владельца (решение владельца продукта — см.
AI_RELEASE_DECISIONS.md), и без этого решения ничего стороннего не заводится.
