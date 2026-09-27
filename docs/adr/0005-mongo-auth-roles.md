# ADR-0005: Mongo `--auth` и least-privilege роли (R26-07)

Дата: 2026-09-27. Статус: принят. Код: `voice_tracker/migrate.py` (ROLE_PLAN/
USER_PLAN/ROOT_ROLE_PLAN/`ensure_roles`/`ensure_users`/`bootstrap_users`/CLI
`users --bootstrap`),
`voice_tracker/repository.py` (`verify_startup`), `voice_tracker/runtime.py`
(`DSBOT_SCHEMA_MODE`), `deploy/*/compose*.yml` (mongod `--auth`, сервис
`mongo-bootstrap` под профилем `bootstrap`), `deploy/scripts/validate_env.py` /
`validate_compose.py` (контракт env/render), `deploy/backup/` (URI из env-файла).

## Контекст
ADR-0003 объявил DB06 («у runtime-пользователей нет DDL») на встроенной роли
`readWrite` — и это было неверно: `readWrite` РЕАЛЬНО разрешает
createIndex/dropIndex/dropCollection (проверено `rolesInfo` на Mongo 7). Пока
mongod работал без `--auth`, ролевая модель была декоративной вовсе: сервер не
сверяет привилегии неаутентифицированных соединений, и «least privilege»
держался только на честном слове. DB06 требует двух условий одновременно:
**auth на сервере** и **состав грантов уже, чем readWrite**.

## Решение
1. **Единственный источник состава привилегий — `ROLE_PLAN`/`USER_PLAN` в
   `voice_tracker/migrate.py`.** Кастомные роли рабочей БД:
   `dsbot_runtime_bot_role` / `dsbot_runtime_web_role` — CRUD+чтение метаданных
   (`find, insert, update, remove, listCollections, listIndexes,
   collStats, dbStats, killCursors`), БЕЗ какого-либо DDL;
   `dsbot_migration_role` — то же + созидательная часть
   (`createIndex, dropIndex, createCollection, collMod`). Пользователи:
   `dsbot_app`/`dsbot_web` — только свои runtime-роли, `dsbot_migration` —
   runner-роль; `createCollection` runtime не нужен — коллекции существуют к
   моменту первой записи (их создал runner; implicit-create не входит в план).
   `getMore` в плане нет сознательно: отдельной серверной привилегией
   continuation курсора не является (авторизуется правами исходной
   `find`/`aggregate`), а `createRole` на mongo:7 отвергает её как
   `Unrecognized action: getMore` (живой прогон r2607-стенда).
   Декларативно план сверяют unit-тесты, enforcement доказывает auth-стенд
   (п.7).
2. **DDL — только runner'у.** Runtime стартует в режиме verify:
   `DSBOT_SCHEMA_MODE=verify` (дефолт; неизвестное значение — падение старта,
   fail-closed) → `repository.verify_startup`: каноническая сверка индексов
   T10 без единой DDL-команды. `bootstrap`-режим — только dev / первый прогон
   job-runner'а. Рабочий порядок на чистой БД: bootstrap прав → `migrate up`
   под `dsbot_migration` → запуск приложений под verify.
3. **Начальные права — localhost exception на пустом томе.** `mongod --auth`
   до появления первого пользователя принимает одно неаутентифицированное
   соединение с 127.0.0.1, создающее админа. Для этого в compose есть
   ОДНОРАЗОВЫЙ сервис `mongo-bootstrap`: `network_mode: service:mongo`
   (localhost exception только из сетевого namespace mongo), профиль `bootstrap`
   (в обычный `up` не входит — иначе ломал бы повторные up), `restart: "no"`
   (одноразовый job). Запуск: `docker compose ... --profile bootstrap run
   mongo-bootstrap python -m voice_tracker.migrate users --bootstrap` — пароли
   берутся из env-файла (`DB_USER_ROOT`/`DB_PASS_ROOT` — имя/пароль root;
   пароли пользователей плана — `DB_USER_<USERNAME в ВЕРХНИЙ РЕГИСТР>`), в
   вывод идут только имена созданных сущностей.
4. **Grants-репарация идемпотентна и локаут-безопасна.** `ensure_users` после
   createUser/upsert-пути сверяет grants через `usersInfo`, а состав кастомных
   ролей — через `rolesInfo`: избыточные роли (например leftover встроенной
   `readWrite` с её createIndex/dropIndex/dropCollection — ровно DB06-нарушение
   прошлого) ОТЗЫВАЮТСЯ, недостающие выдаются, съехавший состав роли
   перезаписывается `updateRole`. Изменяются ТОЛЬКО пользователи плана
   `dsbot_*`: root/admin и пользователи вне плана не понижаются никогда
   (реверк идёт поимённо), пароли существующих не ротируются.
5. **Бэкап/восстановление — отдельными встроенными ролями в `admin`** (не
   runtime-ролями рабочей БД): `dsbot_backup` — `backup@admin` (только
   mongodump, без записи); `dsbot_restore` — `restore@admin` +
   `readAnyDatabase@admin` (mongorestore + чтение целей гейтами restore.sh).
   `deploy/backup/*` берут URI из env-файла: `MONGO_BACKUP_URI` (dump),
   `MONGO_RESTORE_URI` (restore), `MONGO_ADMIN_URI` (mongosh-гейты
   listDatabases/drop); отсутствие значения = отказ скрипта до попыток
   подключения. Контейнерный healthcheck (`mongosh ping`) не меняется: ping —
   auth-exempt команда.
6. **auth НЕ зависит от replica set.** `--auth` включён на standalone-контейнере
   этого же проекта; enforcement одинаков для любой топологии. Replica set
   добавится в R26-09 БЕЗ смены модели прав: те же роли/пользователи/URI,
   только seeds в URI. Миграция на внешний Mongo — тоже только смена URI в
   env-файле, план прав не меняется.
7. **Enforcement доказывается стендом, а не верой.** Одноразовый auth-стенд
   `deploy/scripts/r2607_auth_stand.sh` (mongo:7 `--auth` на
   127.0.0.1:27098, БД `voice_tracker_t07auth_<hex>`) создаёт начальные права
   ТОЧНОЙ production-точкой входа — `python -m voice_tracker.migrate users
   --bootstrap` (bootstrap_users → localhost exception → ROOT_ROLE_PLAN/
   ROLE_PLAN/USER_PLAN) в helper-контейнере с общим сетевым namespace mongod;
   самописного mongosh-генератора плана больше нет — стенд не может «спрятать»
   расхождение с продакшн-комплектацией.
   `tests/test_mongo_auth_stand.py`: CRUD/listIndexes runtime'ом проходят,
   ЛЮБОЙ DDL/админ-команда отбивается РЕАЛЬНЫМ кодом сервера 13 (Unauthorized)
   — это и есть доказательство DB06; migration-роль строит индекс; у
   `dsbot_app` нет admin-ролей; grants-репарация отзывает leftover `readWrite`;
   backup/restore-URI живой аутентификацией подтверждают authSource=рабочая БД
   (с authSource=admin сервер ОТВЕРГАЕТ, code 18); roles root'а на сервере
   равны ROOT_ROLE_PLAN, root делает dropDatabase, повторный production
   bootstrap идемпотентен.
   Общий стенд 27099 (без auth) остаётся для runner'а/потоков; allowlist
   стендовых портов — `tests/stand_guard.py`.

## Ротация паролей
Ротация — явный шаг оператора, не часть `ensure_users` (пароли существующих он
не меняет): (1) сгенерировать новый пароль; (2) admin-сессией
`runCommand({updateUser: "<user>", pwd: "<новый>", roles: [...]})` в той БД,
где пользователь создан (authSource); (3) заменить credential-компонент
соответствующего `MONGO_*_URI` в env-файле профиля; (4) `deploy.sh <profile>
--apply` (или рестарт контейнеров) — старые соединения живут до рестарта,
новые подключения со старым паролем сервер отвергает сразу после шага 2,
поэтому 2→3→4 выполняются в одном окне. Проверка: preflight прогоняет render-
инварианты (бот-URI ровно `MONGO_BOT_URI`, web — `MONGO_WEB_URI`).

## Последствия
- **Render/env-контракт стал строже.** `validate_compose.py` отвергает mongod
  без `--auth`, безпарольный `mongodb://mongo[:порт]` в любом service.env и
  расхождение `MONGO_URI` бота/web с `MONGO_BOT_URI`/`MONGO_WEB_URI` выбранного
  env-файла; `validate_env.py` требует новые обязательные ключи и отвергает
  staging-URI с `authSource=voice_tracker`. Старый безпарольный env не
  заведётся никогда (fail-closed на preflight).
- **Минимальные привилегии root (учётка localhost exception).** ADR фиксирует
  назначение root: начальный bootstrap (`migrate users --bootstrap`) и
  админ-гейты restore.sh (`listDatabases`/`dropDatabase` цели в cleanup).
  `userAdminAnyDatabase` для этого недостаточно (он не даёт ни listDatabases,
  ни drop чужих БД). Единственный источник состава — `migrate.ROOT_ROLE_PLAN`
  (`userAdminAnyDatabase` + `readWriteAnyDatabase` + `dbAdminAnyDatabase` +
  `backup` + `restore` + `clusterMonitor`): dbAdminAnyDatabase даёт dropDatabase
  cleanup'а и DDL-грант для createRole миграционной роли, readWriteAnyDatabase —
  CRUD-привилегии, которыми обязан владеть грантер, backup/restore/clusterMonitor —
  выдачу соответствующих built-in ролей планом и listDatabases-гейт restore.sh
  (эмпирически подтверждено живым прогоном стенда на mongo:7). root вне
  `USER_PLAN` — `ensure_users` его не понижает.
- **Приложения больше не могут чинить схему сами.** Missing/incompatible
  индексы на startup в verify-режиме — падение с диагностикой, а не тихий
  createIndex; лечение — `migrate up` под migration-пользователем. Это и есть
  разделение responsibilities из ADR-0003 («смена least-privilege пользователями
  переведёт приложения в чистый verify-режим»).
- **web-копия контракта не затронута.** R26-07 меняет только ROLE_PLAN/USER_PLAN
  (пользователи/роли) — `MANIFEST` индексов в `voice_tracker/schema.py` НЕ
  менялся, checksum прежний, синхронизация `api/schema_contract.py` /
  `api/schema_manifest.json` в wt-web НЕ требуется. При будущих сменах манифеста
  порядок не меняется: экспорт `migrate export-manifest` + микро-PR в web с
  копией и сверкой checksum (сторожится тестом рассинхрона копий).
- **Прозрачность для NATS/веба не меняется**: auth касается только Mongo;
  NATS остаётся без учётных данных в internal-сети (отдельное решение, не
  R26-07).
