# dsbot deploy (T13)

Переносимая инсталляция: **production** (Linux-цель) и **staging** (тот же
артефакт на текущем Windows-хосте, изолированные данные). Решением владельца
проверка идёт сначала на Windows-стенде; успех стенда НЕ объявляет готовым
Linux-production-релизом (P04–P06 на реальном Linux проверяются при переносе).

## Макет
```
deploy/
  production/compose.yml      # прод: только immutable images, свои mongo/nats, ingress web loopback
  production/env.example      # шаблон env (ключи; значений секретов в git нет)
  staging/compose.staging.yml # те же образы; свои volumes/БД/порт — P03
  staging/env.staging.example
  scripts/                    # preflight / deploy / status / rollback / make_manifest / linux_init / валидаторы
  manifest/                   # манифесты «что задеплоено»; current.json — указатель
```

## Правила
- **Никогда** не запускайте `docker compose` из случайного cwd: только скрипты,
  они прописывают `-p <project> -f <файл> --env-file <env>` явно.
- Секреты живут только в env-файле на хосте (`deploy/<profile>/.env`, chmod 600),
  в git — только `.example`. Значения не печатаются ни одним скриптом.
- Образы — **digest'ы** (`registry/name@sha256:...`), не теги. Тег `v...`/`0.2.0`
  — человекочитаемое имя, deploy-источник — манифест + env.
- `WEB_DEV_BYPASS_AUTH` в этих профилях не существует (конфиг web отвергает его,
  validate_env — тоже).
- controlplane выключен по умолчанию (ADR-0004, профиль `controlplane`).
- Mongo/NATS — контейнеры этого же проекта на internal-сети; наружу публикуется
  только web на **loopback** (TLS/публичный интерфейс — реверс-прокси хоста).
  Переезд с Windows-host Mongo (:27017) — отдельная backup/restore миграция (T14),
  не молчаливая смена URI.

## Первый запуск (Linux-хост)
1. Установить docker engine + compose plugin; `systemctl enable --now docker`
   — docker стартует от root-сервиса systemd; контейнеры поднимает политика
   `restart: unless-stopped` после старта демона (P06).
2. Положить `deploy/` на хост (checkout исходников не нужен) и создать
   `deploy/production/.env` из `env.example`; digest'ы — из релизного манифеста.
3. `scripts/preflight.sh production` — инварианты compose/env + существование и
   platform-вариант каждого образа (п.9).
4. `scripts/linux_init.sh production` — тома + **проверка записи в media от
   ожидаемого uid/gid** (P05: факт, а не имя volume) + mongo-том.
5. `scripts/deploy.sh production` — план (dry-run), затем `scripts/deploy.sh
   production --apply` (pull по digest + up).
6. `scripts/status.sh production` — web `/api/readyz`, heartbeat'ы сервисов.
7. Проверка web-media-ro: из контейнера web запись в `/data/media` обязаны быть
   невозможны (`ro`-монт); чтение — работать.
8. `scripts/make_manifest.sh production` — зафиксировать, что именно работает.
   В include-отчёт релиза: SHA обоих приложений (из OCI-меток), digest'ы,
   schemaVersion/манифест-чексумма/event версия, дата, платформа, previous.

## Windows staging (текущий хост)
Те же шаги с `staging`: отдельный compose-проект `dsbot-staging`, свои тома,
база `voice_tracker_staging`, порт 8090. Прод-контейнеры (dashboard-clone)
не трогаются: имена проектов/томов/сетей различаются, validate_env блокирует
staging-env с прод-идентичностями (P03).

## Логи / диагностика
- `docker compose -p <project> -f <файл> logs --since 30m <service>`;
  json-file с ротацией (10m×5) задан в compose — диск не съедается.
- Структурные строки `supervise event=...` и `diagnostics ...` — см.
  `docs/runbook-health.md`.
- Heartbeat'ы: `docker compose exec <service> python -m voice_tracker.healthcheck`.

## Откат (P09)
`scripts/rollback.sh <profile> <manifest.json>` — показывает разницу образов,
требует подтверждения, сверяется с schemaVersion в БД и **блокирует откат под
более новую схему данных** (миграции additive, но старое приложение не
обязано понимать новые структуры). Откат ПОСЛЕ первой записи в новой схеме —
это backup/restore (T14), а не смена образов.

## Чего здесь сознательно нет
- Legacy BFF/UI/workers из dashboard-mvp — не мигрируют (старая инсталляция,
  судьба — T18 cutover).
- «unhealthy → авто-рестарт» — нет автохилера (restart storm); рестарт — у
  restart policy, health — у оператора/монитора.
- Alerting-канал — ждёт решения владельца (см. runbook-health).
