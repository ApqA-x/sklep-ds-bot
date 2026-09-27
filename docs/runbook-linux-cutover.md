# Runbook: cutover staging(Windows) → Linux production (R26-14)

Пошаговый пакет переноса боевой инсталляции с текущего Windows-хоста на Linux.
Детали backup/restore здесь НЕ дублируются — источник истины по снимкам,
режимам и state-файлам: `docs/runbook-backup.md`; по скриптам развёртывания:
`deploy/README.md`.

## 1. Статус и границы

- **Сам cutover BLOCKED на D12**: Linux-хост прода ещё не предоставлен
  (статус внешних зависимостей ведётся во внешнем трекере, файл
  `EXTERNAL_DEPS_STATUS_2026-09-27.md` — в этом checkout репозитория его НЕТ,
  это артефакт трекера). Пока нет хоста и согласованного окна (карточка
  D09-details), исполнение шагов 1–9 раздела 3 невозможно.
- Этот документ — **готовый исполнимый пакет**: активация только после
  появления хоста (D12) и подтверждения окна оператором.
- Не отменяет и не дублирует `docs/runbook-backup.md`: раздел
  «Перенос Windows → Linux» там — источник деталей по backup/restore; здесь —
  полный порядок cutover со сверенными с репозиторием командами.
- Все команды — от корня checkout на соответствующем хосте (путь-пример
  `/opt/dsbot`, как в `docs/runbook-backup.md`; на хост кладётся каталог
  `deploy/`, checkout исходников не нужен — `deploy/README.md`). Скрипты
  вызываются только как `deploy/scripts/...` и `deploy/backup/...` с явным
  профилем `production`; «голый» `docker compose` допускается лишь в полной
  форме с явными `-p/-f/--env-file` (правило `deploy/README.md`).
- Команды вне `deploy/` (`rsync`, `age`/`age-keygen`, `systemctl`, `stat`,
  `docker ps`) — стандартные утилиты хоста, не скрипты репозитория; помечены.

## 2. Предварительные условия (каждый пункт — проверяемое утверждение)

1. **D12 закрыт**: есть Linux-хост — известён адрес, подтверждена ОС и версия
   ядра, есть ssh-доступ под учёткой с правами на docker и systemd, диски
   размечены (отдельный том под `BACKUP_DIR` желателен — см. п.7).
2. **Docker**: Docker Engine + compose v2 plugin установлены; `docker info`
   отвечает; `sudo systemctl enable --now docker` выполнено (P06: docker
   стартует от root-сервиса systemd, иначе после reboot контейнеры не
   поднимутся сами — заметка это требует и `deploy/scripts/linux_init.sh`).
   Для `preflight.sh` нужен `docker buildx` (проверка digest/platform через
   `docker buildx imagetools inspect`); для приватного ghcr — выполненный
   `docker login ghcr.io`.
3. **Mongo (решение R26-07 принято и зафиксировано в коде — ADR-0005)**: контейнер этого же
   проекта на internal-сети, без published ports, но **под `--auth`**
   (`deploy/production/compose.yml`). Все URI приходят из env-файла:
   `MONGO_BOT_URI`/`MONGO_WEB_URI` (runtime, без DDL-роли), `MONGO_MIGRATION_URI`
   (compose-сервис `schema-migrate`, профиль `migrate` — единственная легальная
   точка `migrate up`/`status`), `MONGO_ADMIN_URI`
   (гейты mongosh), `MONGO_BACKUP_URI`/`MONGO_RESTORE_URI` (backup/restore);
   начальные права на пустом томе создаёт `python -m voice_tracker.migrate
   users --bootstrap` (localhost exception, профиль compose `bootstrap`;
   пароли `DB_USER_*`/`DB_PASS_ROOT` из env), роли least-privilege и их
   grants-сверка — `migrate users` (DB06). Молча подменять URI нельзя:
   сверка env-ключей с рендером — в `validate_compose.py`.
4. **NATS**: то же решение, что и по Mongo — в текущем compose это контейнер
   проекта (`nats://nats:4222`, internal-сеть, мониторинг `:8222` только
   внутри сети). Отдельного env-ключа для NATS_URL нет.
5. **DNS/TLS/реверс-прокси (D08)**: web-ingress публикуется **только на
   loopback** (`ports: "${WEB_INGRESS_BIND:-127.0.0.1}:${WEB_HOST_PORT:-8000}:8000"`);
   публичный HTTPS — ответственность реверс-прокси хоста. До smoke-шага
   должны быть: DNS-имя хоста, TLS-сертификат в реверс-прокси, совпадающие
   `WEB_PUBLIC_URL`/`DISCORD_REDIRECT_URI` в env и в конфиг-приложении Discord.
6. **Env-файл приёмника**: `deploy/production/.env` на Linux создан из
   шаблона `deploy/linux/env.production.example` (в репозитории) или
   `deploy/production/env.example`, заполнен, `chmod 600`; digest'ы образов —
   из финального релизного манифеста (п.8).
7. **age-ключ и копии точек**: `BACKUP_AGE_KEY_FILE` на Linux — тот же
   симметричный ключ, что шифровал точки (D06; `age --decrypt -i`); права 600,
   файл вне git. Публичная часть совпадает: `age-keygen -y <ключ>` (утилита
   возраста) даёт на обоих хостах одно значение. `BACKUP_DIR` на Linux —
   каталог на **независимом** диске/узле относительно данных (тогда и
   закрывается B05; D06 принимал копию на том же диске как временную
   меру — `docs/runbook-backup.md`).
8. **Финальный релизный манифест**: список образов pinned-digest'ами
   (`registry/name@sha256:<64 hex>`, без тегов — P01) зафиксирован;
   `deploy/scripts/preflight.sh production` подтвердит существование каждого
   digest и platform-вариант хоста ещё до любых изменений.

## 3. Последовательность cutover

### Шаг 1. Финальный снимок источника (текущий Windows-хост)

```bash
deploy/backup/backup.sh production
```

(на Windows — в той же WSL-среде, где штатно крутятся бэкапы; путь-пример
`/opt/dsbot`.) Заморозка writers входит в снятие (`compose stop` всего, кроме
`mongo`/`nats`; SIGTERM-drain из T12), после финализации скрипт **поднимает
обратно ровно то, что работало** — это штатное поведение ежедневного прогона.

**Политика cutover:** с этого момента Windows-writers не поднимаются (запрет
двух активных наборов писателей — `docs/runbook-backup.md`, п.10 плана).
Сразу после завершения `backup.sh` остановить набор на старом хосте
(полная форма, `deploy/README.md`) и больше не запускать:

```bash
docker compose -p dsbot-prod -f deploy/production/compose.yml \
  --env-file deploy/production/.env \
  stop gateway tracker writer commands activity stalker web
```

Если на старом хосте остаются писатели старой (dashboard-mvp) инсталляции —
остановить их штатным для той инсталляции способом; контроль — шаг 9.

Точка: `<BACKUP_DIR>/production/dsbot-production-<YYYYMMDDTHHMMSSZ>/` со
sidecar `.verified_ok`, манифестом и `.age`-архивами.

### Шаг 2. Перенос точек и preflight на приёмнике (без prod-доступа)

Копирование последней точки на Linux (утилита хоста):

```bash
rsync -a "$BACKUP_DIR/production/dsbot-production-<TS>/" \
  ops@linux-host:/var/backups/dsbot/production/dsbot-production-<TS>/
```

Контроль целостности транспорта не требуется как отдельный шаг:
`restore.sh` сам сверяет манифест и sha256 шифрованных файлов **до первой
записи** (B04; `verify_checksums` в `deploy/backup/restore.sh`).

Сверка age-ключа (утилиты возраста, не репо):

```bash
stat -c '%a' /etc/dsbot/age.key          # ожидаем 600
age-keygen -y /etc/dsbot/age.key         # публичная часть = значение со source-хоста
```

Preflight (ничего не меняет; требует docker-демона и доступа к registry):

```bash
deploy/scripts/preflight.sh production
```

Проверяет: env-файл (ключи/формат значений, значения не печатает —
`validate_env.py`), рендер compose и инварианты P01/P02/P07
(`validate_compose.py`), существование каждого pinned-digest и наличие
platform-варианта `linux/<arch>` (`docker buildx imagetools inspect`).

### Шаг 3. Репетиция восстановления на новом стенде (B07, частичное V26-28)

```bash
deploy/scripts/linux_init.sh production
```

Идемпотентно создаёт тома `MONGO_VOLUME`/`MEDIA_VOLUME` из env и **проверяет
запись в media от ожидаемого `DSBOT_UID:DSBOT_GID`** (P05 — факт записи, а не
«volume с именем существует»).

`restore.sh` работает с живой БД-контейнером (`compose exec -T mongo`,
`compose run --rm --no-deps ... gateway`), поэтому для репетиции нужен
запущенный **каркас инфраструктуры**, приложение не запускается вообще
(полная форма команды — `deploy/README.md`):

```bash
docker compose -p dsbot-prod -f deploy/production/compose.yml \
  --env-file deploy/production/.env up -d mongo nats
```

Сразу после подъёма **пустого** тома — одноразовый bootstrap прав (R26-07,
ADR-0005; без него mongod под `--auth` не пустит ни гейты `restore.sh`, ни
приложения, ни сам runner). localhost exception mongod принимает только с
127.0.0.1, поэтому `mongo-bootstrap` идёт в сетевом namespace mongo
(`network_mode: service:mongo`), под явным профилем `bootstrap`; пароли —
`DB_USER_*`/`DB_PASS_ROOT` из того же env-файла:

```bash
docker compose -p dsbot-prod -f deploy/production/compose.yml \
  --env-file deploy/production/.env \
  --profile bootstrap run --rm mongo-bootstrap \
  python -m voice_tracker.migrate users --bootstrap
```

Репетиция в режиме `rehearsal` (по умолчанию): цели генерируются прогоном —
БД `voice_tracker_production_rehearsal_<runid>` (строгий allowlist
`restore_targets.py validate-db`) и новый media-volume
`dsbot-production-restore-media-<runid>` с ownership-меткой; существующая цель
= отказ до любой записи; verify обязателен (`--no-verify` удалён, R26-08).
После успеха прогон убирает свои цели; зафиксированная в отчёте длительность
`restore занял Ns (B07 evidence)` — это замер RTO (цель D06: ≤ 2ч).

```bash
deploy/backup/restore.sh production --mode rehearsal \
  --from /var/backups/dsbot/production/dsbot-production-<TS> \
  --state /var/tmp/dsbot-restore-rehearsal.json
```

Примечание к формулировке карточки: вариант `deploy/backup/restore.sh staging
--mode rehearsal --state /var/tmp/dsbot-restore-staging.json` (команда
дословно из `docs/runbook-backup.md`) корректен, только если на стенде
развёрнут staging-профиль (`deploy/staging/.env`, `linux_init.sh staging`,
запущенный staging-контейнер mongo) и снимок передан через `--from` — иначе
скрипт откажет на отсутствующем env-файле профиля. В rehearsal-режиме профиль
не влияет на безопасность целей (allowlist покрывает и
`voice_tracker_production_rehearsal_…`), поэтому на чистом Linux-стенде
достаточно варианта `production` выше.

Отказ на любом шаге репетиции: повтор тем же `--state --resume`; новый прогон
— с новым `--state` (занятый state-файл блокирует повтор как чужую резервацию
целей). Это же подтверждает частичную закрытость V26-28 (карточка внешнего
трекера; определение в репозиторий не внесено).

### Шаг 4. Восстановление боевой точки на Linux (cutover)

```bash
deploy/backup/restore.sh production --mode cutover \
  --into-db voice_tracker_production --confirm-dest voice_tracker_production \
  --from /var/backups/dsbot/production/dsbot-production-<TS> \
  --state /var/tmp/dsbot-restore-cutover.json
```

Флаги — по usage `deploy/backup/restore.sh` (точное совпадение `--into-db` и
`--confirm-dest` обязательно; иначе cutover не принимает целевое имя).
Особенности режима: `--keep` форсируется скриптом (cleanup и какие-либо
удаления существующих ресурсов в cutover запрещены); цель обязана
**отсутствовать** на сервере — гейт `listDatabases` проверяется дважды (гонка)
перед `mongorestore` без `--drop`; на чистой машине — отсутствует.

**Активация env профиля на восстановленные цели — отдельный осознанный шаг
оператора** (`docs/runbook-backup.md`, режим cutover): в
`deploy/production/.env` установить `MONGO_DB=voice_tracker_production` и
`MEDIA_VOLUME=<dsbot-production-restore-media-<runid> из отчёта restore>`
(имена целей печатаются в отчёте прогона). Media-volume с боевым именем из
исходного env при этом остаётся созданным `linux_init.sh` и неиспользованным
— это ожидаемо, ничего не удалять.

### Шаг 5. Verification без доступа к прод-инстансу

Приложение ещё не поднимается (единственный запущенный контейнер с кодом —
`mongo`; проверки идут через одноразовые контейнеры или `exec` в mongo):

1. Обязательный verify самого restore уже выполнен прогоном шага 4 — counts
   против манифеста, индексы канонической сверкой схемы T10, revision в
   `guild_settings`, наличие каждого media-файла по `attachments.path`,
   schemaVersion. Повторно: без verify прогон не завершается структурно.
2. Схема миграций — read-only статусы раннера T10. **Честно: подкоманды
   `python -m voice_tracker.migrate check` в репозитории НЕ существует**
   (`docs/runbook-backup.md` называет так схемную сверку вообще); фактические
   subcommand'ы `voice_tracker/migrate.py`: `status | plan | up | snapshot |
   export-manifest | users | check-rollback`. Единственная легальная точка для
   них на проде — compose-сервис `schema-migrate` под явным профилем `migrate`
   (review R26-07, blocker 2): его `MONGO_URI` интерполируется ровно из
   `MONGO_MIGRATION_URI` (`dsbot_migration` — единственная роль с DDL), а
   `MONGO_DB` — из env-файла, активированного шагом 4, поэтому `--db` в команде
   не нужен и перебивать его нельзя. Поднимать для этого `gateway` (или любой
   app-сервис) НЕЛЬЗЯ: бот-URI = `dsbot_app` без DDL-роли, и прогон, «работавший»
   на стенде без auth, на live `--auth` даёт Unauthorized; подмена URI сервиса на
   runtime/admin-URI отсекается и render-контрактом `validate_compose.py`.
   Mongo недоступна с хоста (internal-сеть), поэтому из одноразового контейнера
   того же образа (полная форма compose-команды):

```bash
docker compose -p dsbot-prod -f deploy/production/compose.yml \
  --env-file deploy/production/.env \
  --profile migrate run --rm schema-migrate \
  python -m voice_tracker.migrate status
```

   Ожидаем: `latest` = schemaVersion из манифеста точки; ни одной записи
   `running`/`failed` в `migrations` (прерванная миграция на source-хосте —
   блокиратор cutover, разбираться по ADR-0003). `plan` (он же `up --dry-run`)
   показывает, что применялось бы дополнительно — после корректного restore
   ожидание пусто по DDL; если нет, применяется той же командой, заменив
   `status` на `up` (тот же сервис, тот же профиль).

### Шаг 6. Promotion — подъём приложения на Linux

```bash
deploy/scripts/deploy.sh production          # dry-run: план, ничего не трогает
deploy/scripts/deploy.sh production --apply  # preflight → pull по digest → up -d → status
```

Порядок старта — честно про compose: `depends_on` в
`deploy/production/compose.yml` гарантирует только `mongo`/`nats` healthy
**перед любым** app-сервисом; порядка «потребители → gateway» между app-
сервисами compose не задаёт (`up -d` поднимает их параллельно). Требование
«consumers → gateway → entrypoints» (`docs/runbook-backup.md`, п.9–11) при
необходимости жёсткого порядка выполняется поштучно полной compose-командой
(`up -d tracker writer commands activity stalker`, затем `up -d gateway`,
затем `up -d web`). Двойной gateway при этом не грозит: старый набор
остановлен на шаге 1.

**Профиля `publish` в этом репозитории НЕ существует** — единственная
опциональная роль: `docker compose --profile controlplane up -d controlplane`
(ADR-0004; по умолчанию controlplane **выключен**, вне профиля не рендерится
в `compose config --services`; включать только осознанно, с записью кто/зачем,
и только после выполнения условий ADR-0004 — issuer, idempotency, тесты).
Никакого «--profile publish» искать не нужно.

### Шаг 7. Smoke

```bash
deploy/scripts/status.sh production
```

Даёт: `compose ps`, web `GET /api/readyz` на `127.0.0.1:${WEB_HOST_PORT}`
(через curl хоста; если curl нет — контейнерный fallback), heartbeat'ы
gateway/tracker/writer/commands/activity/stalker
(`python -m voice_tracker.healthcheck` внутри контейнеров), свежесть последней
проверенной точки против `BACKUP_MAX_AGE_HOURS` (`deploy/backup/backup_status.sh`,
B06).

Read-only media из web (P05-B; ожидание: запись НЕвозможна, чтение работает):

```bash
docker compose -p dsbot-prod -f deploy/production/compose.yml \
  --env-file deploy/production/.env exec web sh -c \
  'touch /data/media/.write-check && echo "PROBLEM: запись удалась" || echo "OK: media read-only"'
docker compose -p dsbot-prod -f deploy/production/compose.yml \
  --env-file deploy/production/.env exec web ls /data/media | head
```

Outbound Discord: соединение gateway к Discord со старого хоста снято шагом 1,
с нового — проверяется по логам gateway (полная форма, `deploy/README.md`):

```bash
docker compose -p dsbot-prod -f deploy/production/compose.yml \
  --env-file deploy/production/.env logs --since 10m gateway
```

Признаки: gateway-сессия установлена, commands-синхронизация слэш-команд
отработала, в логах нет `4014`/переподключений из-за второго клиента.

Таймеры бэкапа на новом хосте (установка unit'ов `deploy/backup/systemd/` —
по `docs/runbook-backup.md`, «Ежедневный запуск»):

```bash
sudo systemctl enable --now dsbot-backup.timer dsbot-backup-status.timer
systemctl list-timers 'dsbot-backup*'
```

Первый прогон `deploy/backup/backup.sh production` на Linux подтвердит B05 в
новом независимом хранилище.

### Шаг 8. Фиксация

```bash
deploy/scripts/make_manifest.sh production
```

Пишет `deploy/manifest/manifest-<stamp>.json` (symlink `current.json`),
содержимое: digest'ы образов + git SHA из OCI-меток + schemaVersion /
manifest checksum / event версия из кода образа + дата + платформа +
previousManifest (значения секретов не читаются). В отчёт cutover записать:
deployed-digest'ы, SHA обоих приложений, schemaVersion, возраст backup-точки
(`deploy/backup/backup_status.sh production`) и путь state-файла cutover-прогона.

### Шаг 9. «Переключение» Discord-бота и active voice-сессия

Фактической «перемены» отдельным переключателем нет: единственная точка
владения `DISCORD_TOKEN` — это смена хоста, с которого крутится ЕДИНСТВЕННЫЙ
gateway. Grace-drain старого gateway уже обеспечен заморозкой шага 1 (SIGTERM
и корректный drain задач из T12). Контроль перед этим шагом и после:

```bash
# на СТАРОМ (Windows/WSL) хосте — app-контейнеров dsbot-prod нет:
docker ps --format '{{.Names}}' | grep -i dsbot || echo "OK: старый набор остановлен"
```

Активная voice-сессия на границе переноса: интервал между freeze (шаг 1) и
подъёмом gateway (шаг 6) НЕ является точно восстановленной историей —
reconciliation на Linux догонит его по Discord-аудиту (H-механика T11) и
закроет интервал с явной границей; лишний `session.close` запрещён контрактом
`event_id` (T09). Пауза не «дорисовывается» задним числом. Детали —
`docs/runbook-backup.md`, раздел «Перенос Windows → Linux», п.5 (п.11 плана).
После подтверждения шага 7 ограничения снимаются и происходит первая запись
на Linux — с этого момента применяется сценарий отката B.

## 4. Откат-процедура (два разных сценария, как в rollback.sh / P09)

`deploy/scripts/rollback.sh production <deploy/manifest/manifest-....json>`
— показывает разницу образов, требует подтверждения вводом `ROLLBACK`
(или `--force`), патчит образы в env-файл (с бэкапом `.env.rollback-bak-<ts>`),
сверяется с `schema_versions` в БД (только чтение) и **блокирует откат под
более новую схему данных**; затем сам вызывает `deploy.sh --apply` и
`make_manifest.sh`.

**A) Откат до первой записи на Linux** (данные = слепок restore, новых записей
нет): возврат образов на прежний манифест через `rollback.sh` безопасен;
schemaVersion-гейт при этом не даёт укатиться под схему, уже записанную в БД.
Старый (Windows) набор всё это время **остаётся замороженным** — не поднимать
(п.10 плана: два набора писателей недопустимы). Возврат самого состояния
данных, если оно испорчено, — только restore свежей Linux-точки
(`deploy/backup/backup.sh production` на Linux → перенос → `restore.sh` с
генерируемыми целями), а не «restart старого контейнера».

**B) Откат после первой записи на Linux**: простой возврат образов
(`rollback.sh`) НЕ возвращает данные — записи новой версии останутся (схемы
additive/backward-compatible, но старое приложение не обязано понимать новые
структуры; schemaVersion-гейт это ловит). Возврат на Windows возможен только
как **обратная миграция данных**: `deploy/backup/backup.sh production` на
Linux → перенос точки на старый хост → `restore.sh` в режиме cutover на
Windows-стенде в боевые имена. Явно: **два production gateway одновременно
держать запрещено** — перед стартом набора на одном хосте набор на другом
остановлен.

## 5. Чек-лист приёмки R26-14 (только на реальном Linux-хосте)

Docker Desktop с Linux-контейнерами на Windows-машине **НЕ заменяет ни один
шаг** — стенд должен быть на реальном Linux-хосте (решение владельца из
`deploy/README.md`: успех Windows-стенда не объявляет Linux-production
готовым; P04–P06 проверяются при переносе).

- [ ] P04 — `deploy/scripts/linux_init.sh production` на реальном хосте: тома
      созданы, запись в media от `DSBOT_UID:DSBOT_GID` подтверждена действием.
- [ ] P06 — `systemctl enable --now docker`; после тестовой перезагрузки
      хоста контейнеры поднялись политикой `restart: unless-stopped` сами.
- [ ] P08 — web-ingress: bind только на `127.0.0.1:${WEB_HOST_PORT}`,
      публичный доступ — через реверс-прокси с TLS (D08); наружу из compose
      ничего, кроме web-loopback, не опубликовано (проверяется
      `validate_compose.py` на шаге 2 и портами хоста). *Определение карточки
      P08 в репозиторий не внесено — закрыть по определению внешнего трекера.*
- [ ] P09 — `rollback.sh` на реальном стенде: демонстрация сверки
      schemaVersion и блокировки отката под более новую схему (сценарий A).
- [ ] B04 — checksums/манифест проверены до первой записи (шаг 4 — часть
      механики restore; зафиксировать в отчёте).
- [ ] B05 — `BACKUP_DIR` на Linux — независимое хранилище (другой диск/узел),
      первая точка снята на Linux и прошла `backup_status.sh`.
- [ ] B07 — замеры длительности rehearsal-restore и cutover-restore
      (строка `restore занял Ns (B07 evidence)` в отчёте прогонов) против цели
      RTO ≤ 2ч (D06); расхождение пишется в отчёт как есть.
- [ ] V26-28 — частичное: подтверждено rehearsal-циклом шага 3 на новом стенде
      (окончательное закрытие — по определению внешней карточки).

До подтверждения всех пунктов релиз-статус остаётся **candidate/staged**, не
released. Блокираторы, не снимаемые этим пакетом: D12 (хост), D09-details
(окно). Решение R26-07 снято: Mongo остаётся контейнером compose, но под
`--auth` с env-URI и least-privilege ролями (ADR-0005).
