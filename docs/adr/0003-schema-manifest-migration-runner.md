# ADR-0003: Канонический manifest индексов и migration runner (T10)

## Статус
Принят. Код: `voice_tracker/schema.py` (манифест/сверка), `voice_tracker/migrate.py`
(runner), `deploy/schema_manifest.json` (канонический артефакт), web-зеркало
`api/schema_manifest.json` + `api/schema_contract.py`.

## Почему свой runner (T10.4 — «сначала проверь существующее»)
В обоих репозиториях DDL был размазан по startup (`repository.ensure_indexes` у бота,
`ensure_web_indexes` у веба) — ни alembic, ни Flyway, ни именованных миграций не
существовало; Mongo не имеет встроенного versioning'а индексов. Минимальный честный
механизм: versioned manifest + одношаговый runner с lease-локом, статус-документами
и checksum'ом — без фреймворка.

## Контракт
- Единственный источник правды — `MANIFEST` в `voice_tracker/schema.py`
  (`schemaVersion`, checksum sha256). `deploy/schema_manifest.json` — его экспорт;
  тест `test_deployed_manifest_file_in_sync_with_code` сторожит рассинхрон кода и
  артефакта, web-тест `test_manifest_copy_checksum_matches_bot_source_of_truth` —
  рассинхрон копий между репозиториями.
- Эквивалентность определяется по СПЕЦИФИКАЦИИ: ordered keys, unique, sparse,
  partialFilterExpression, expireAfterSeconds (collation в проде не используется —
  сверяется молчанием имени). Имя — не часть эквивалентности (DB01).
- Одинаковые ключи, разные флаги = **несовместимость**: startup её raises'ит,
  runner отказывает до DDL. Молча не чинится (DB02).

## Прод-снимок (T10.1, read-only, 2026-09-25)
`docs/schema/prod-indexes-2026-09-25.json` — 18 коллекций, только метаданные
индексов, без документов. Итоги сверки:
- 37/41 bot+shared индексов уже в проде эквивалентны; отсутствуют ровно 4 новых
  T09 (event_log/event_inbox) и 2 web operations — создаются до T18 легаси-путём
  startup'а, после T18 — runner'ом.
- **DB01 на реальном проде**: `voice_sessions.web_guild_status_endedAt`
  [guildId,status,endedAt-1] эквивалентен манифестному `web_guildId_status_endedAt`
  → принят как есть, НЕ удаляется и НЕ пересоздаётся ради канонического имени (T10.2).
- Два прод-индекса вне кода (`voice_session_participants.web_guild_active_user`,
  `web_guild_joinedAt`) внесены как `owner="legacy"`: документированы, не создаются,
  не требуются на startup; drop — только отдельным управляемым шагом (T10.7).
- `discord_audit_logs.web_disc_audit_guildId_entryId` (не-unique) — предшественник
  M3; после M3 unique строится отдельным именем, старый — на ручной drop после
  подтверждения.

## Роли startup'ов
- Приложения **не удаляют и не пересоздают** индексы. Bot `ensure_indexes` =
  идемпотентное создание своего набора (legacy-путь до T18) + строгая сверка
  bot/shared: несовместимость → падение (нарушения не скрываются).
  Web lifespan = `ensure_web_indexes` + `verify_web_schema` (web/shared):
  incompatible → исключение, missing → warning/error по `app_env`.
- Смена least-privilege пользователями (ниже) переведёт приложения в чистый
  verify-режим: readWrite не умеет createIndex, создание останется runner'у.

## Runner
`python -m voice_tracker.migrate <status|plan|up [--only N] [--dry-run]|snapshot|users|check-rollback>`

- Миграции: M1 baseline-contract (alias-осознанное создание, DB01),
  M2 operations TTL 90d (additive), M3 discord-audit unique (DB05: сначала
  read-only отчёт дублей; **неодинаковые** документы не удаляются — abort;
  доказуемо эквивалентные мержатся keep-min, затем build unique).
- Статусы: `schema_migrations` {_id, name, checksum, status running|done|failed,
  startedAt/finishedAt, report, error}; `schema_versions` — версия+checksum
  манифеста. Повтор `up` идемпотентен (DB03); смена checksum при done — recheck,
  не переигрывание.
- Lock: `schema_lock` lease 120 s, CAS-перехват просроченного; живой лок
  отбивает второй процесс (DB04).
- **Восстановление после прерывания (T10.4)**: `migrate status` покажет
  `running/failed` с startedAt и ошибкой. Все шаги идемпотентны: DDL create —
  noop при существующем эквиваленте, merge-удаление фильтрует по полному ключу.
  Тупик — только M3 при конфликтных дублях: отчёт в status, действия оператора —
  разрешить дубли вручную и повторить `up --only 3`. `drop` индексов runner не
  делает никогда (только явная будущая миграция с обоснованием).
- Rollback-preflight (DB07): `check-rollback --to-version N` блокирует откат, если
  выше N есть применённая backward-incompatible миграция (M3: уникальный индекс
  ломает запись старых версий) или незавершённая (running/failed).

## Least-privilege пользователи (DB06)
`migrate users` (пароли из env `DB_USER_DSBOT_*`, секреты не в коде):
| пользователь | роль | может | не может |
|---|---|---|---|
| dsbot_app | readWrite (db) | CRUD коллекций | createIndex/dropIndex/DDL |
| dsbot_web | readWrite (db) | CRUD + операции | DDL, админ |
| dsbot_migration | dsbot_migration_role (createIndex, listIndexes, collMod, find, insert, update, remove + read) | строить контракт/миграции | dropIndex/dropCollection, usersInfo-чужих, admin |
| dsbot_backup | backup (admin) | mongodump для backup | запись в БД |
Restore — отдельный временный пользователь dbOwner на время mongorestore (T14).
Стенд (27099) поднимает пользователей без --auth — тест проверяет корректность
ролевых документов; enforcement отказа DDL — T13 на изолированном auth-Mongo
(T10.9: действующий прод-mongod на Windows в ходе разработки не перенастраивается).

## Совместимость чтения/записи (T10.6)
- M1/M2 additive (новые индексы/TTL): старый код работает — rollback разрешён.
- M3 сужает доступ (unique): старый код, писавший дубли, начнёт падать на вставке →
  rollback приложения ниже M3 **запрещён** preflight'ом; совместимый откат — только
  до явной future-миграции drop unique (управляемый шаг).
- TTL операций 90d: replay-окно идемпотентности не дольше срока хранения журнала —
  ключи старше окна не дают «терминального реплея», это ожидаемое сжатие истории.
- Partial unique active-session/participant (инвариант «одна active-сессия на канал/
  участника») в манифесте без изменений; трогать можно только с тестами join/move/
  restart (T10.7).

## Retention (хвост T09)
event_log/event_inbox — без TTL (должны переживать любого потребителя): очистка
завершённых строк inbox старше 30 дней — будущая явная миграция/prune-команда,
не фоновый процесс приложений.
