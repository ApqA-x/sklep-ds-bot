#!/usr/bin/env bash
# R26-07: ОДНОРАЗОВЫЙ auth-стенд для enforcement-проверки модели прав (DB06).
#
# Поднимает mongo:7 с --auth на 127.0.0.1:27098 и прогоняет по чистому кластеру
# ТОЧНУЮ ПРОДАКШН-ТОЧКУ ВХОДА начальных прав: `python -m
# voice_tracker.migrate users --bootstrap` (voice_tracker.migrate.bootstrap_users
# -> ROOT_ROLE_PLAN/ROLE_PLAN/USER_PLAN). helper-контейнер (python:3.12-slim,
# pymongo ставится pip'ом в эфемерную ФС) расшаривает сетевой namespace mongo
# (--network container:$CNAME), поэтому 127.0.0.1:27017 внутри него — ЧЕСТНЫЙ
# loopback mongod и localhost exception срабатывает ровно так же, как на проде
# (review R26-07, blocker 2: состав root обязан доказываться production-путём,
# а не самописным mongosh-генератором, который мог «спрятать» расхождение).
# Целевая БД — voice_tracker_t07auth_<hex> (одноразовая, под стендовый guard
# имён). Скрипта ролей из-под хоста больше нет: plan создаёт production-код.
#
# usage:
#   eval "$(deploy/scripts/r2607_auth_stand.sh up)"   # stdout — только export'ы
#   python -B -m pytest tests/test_mongo_auth_stand.py -q -m integration
#   deploy/scripts/r2607_auth_stand.sh --down
#
# Переопределения: DOCKER_BIN (путь к docker), R2607_IMAGE (образ mongod),
# R2607_PY_IMAGE (образ helper'а), R2607_STATE (файл имени контейнера),
# DB_USER_ROOT/DB_PASS_ROOT и DB_USER_DSBOT_* — именованные креды (по
# умолчанию — случайный hex).
#
# Гигиена секретов: пароли — случайный hex (или значения env-параметров).
# В helper они уходят ТОЛЬКО по stdin (docker exec -i) в файл 0600 внутри
# эфемерной ФС контейнера, который стирается до запуска python и исчезает
# вместе с контейнером (явный rm по trap + --rm + ограниченный sleep).
# Сознательно НЕ используются: (а) `docker run -e KEY` — Windows docker.exe из
# WSL не получает WSL-export по имени (падение живого прогона 2026-09-27:
# «users --bootstrap требует DB_USER_ROOT и DB_PASS_ROOT»), (б) `-e KEY=value`
# и positional-аргументы — секреты в argv, (в) env-файл на диске хоста — его
# docker.exe не прочитал бы по WSL-пути (та же причина, по которой здесь нет
# bind-mount), а любой файл на диске оставляет секреты при ошибке.
# printf — builtin bash'а, значения не попадают в argv отдельного процесса.
# Наружу выходят ТОЛЬКО export-строки URI в stdout для eval; на диск хоста
# пишется лишь state-файл имени контейнера (chmod 600, без секретов). Из stdout
# helper'а (JSON имён созданных сущностей) ничего не печатается в stdout стенда
# — весь вывод helper-фазы уводится в stderr.
#
# Уборка: --down удаляет контейнер вместе с anonymous volume (-v), т.е. БЕЗ
# остатка данных, и заодно leftover helper-контейнер `${CNAME}h` от прерванного
# up (best-effort). Сам helper снимается в том же up: trap по EXIT/INT/TERM +
# --rm + ограниченный sleep, поэтому креды не переживают завершение скрипта;
# стендовая БД к тому же сносится dropDatabase в teardown интеграционного
# модуля (tests/test_mongo_auth_stand.py).
set -euo pipefail
# shellcheck source=_common.sh
source "$(dirname "$0")/_common.sh"

DOCKER_BIN="${DOCKER_BIN:-/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe}"
IMAGE="${R2607_IMAGE:-mongo:7}"
# образ helper'а для ТОЧНОЙ production-точки входа (python + pip); расшаривает
# сетевой namespace mongod (аналог mongo-bootstrap c network_mode: service:mongo
# в боевом compose), поэтому 127.0.0.1 внутри него — честный loopback mongod.
PY_IMAGE="${R2607_PY_IMAGE:-python:3.12-slim}"
PORT=27098                       # стендовый auth-порт; 27017/27018 (прод/dev) запрещены
STATE_FILE="${R2607_STATE:-/tmp/r2607_stand.name}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

rnd() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

usage_stand() {
  cat >&2 <<'EOF'
usage: r2607_auth_stand.sh [--down | -h]
  без аргументов — up: контейнер r2607_<hex> (mongo:7 --auth, 127.0.0.1:27098),
  начальные права (root по ROOT_ROLE_PLAN + роли/пользователи ROLE_PLAN/
  USER_PLAN) создаёт production-команда `python -m voice_tracker.migrate
  users --bootstrap` в helper-контейнере с network namespace mongod;
  в stdout — export-строки URI для eval. --down — удалить контейнер и его
  anonymous volume из state.
EOF
}

have_docker() {
  [ -f "$DOCKER_BIN" ] || command -v "$DOCKER_BIN" >/dev/null 2>&1 \
    || die "docker not found: $DOCKER_BIN (переопределение: DOCKER_BIN=/path/to/docker)"
}

# --------------------------------------------------------------- режим --down
case "${1:-up}" in
  -h|--help) usage_stand; exit 0 ;;
  --down)
    have_docker
    name=""
    [ -f "$STATE_FILE" ] && name="$(head -1 "$STATE_FILE" | tr -d '[:space:]')"
    [ -n "$name" ] || die "нет state-файла $STATE_FILE — удалять нечего (имя контейнера не известно)"
    case "$name" in
      r2607_*) ;;
      *) die "state $STATE_FILE содержит чужое имя ($name) — rm отказан (защита от произвольного контейнера)" ;;
    esac
    "$DOCKER_BIN" rm -f -v "$name" >/dev/null \
      || die "docker rm -f -v $name не удалось (volume тоже не убран — проверить вручную)"
    # leftover helper-контейнера (r2607_<hex>h) от прерванного up: снимается
    # best-effort, его отсутствие — норма (up убирает свой helper по trap)
    "$DOCKER_BIN" rm -f "${name}h" >/dev/null 2>&1 || true
    rm -f "$STATE_FILE"
    info "auth стенд снят (контейнер + anonymous volume): $name"
    exit 0
    ;;
  up) : ;;
  *) usage_stand; die "неизвестный режим $1" ;;
esac

have_docker
[ ! -e "$STATE_FILE" ] || {
  old="$(head -1 "$STATE_FILE" | tr -d '[:space:]')"
  if [ -n "$old" ] && "$DOCKER_BIN" inspect "$old" >/dev/null 2>&1; then
    die "стенд уже поднят ($old): сначала $0 --down"
  fi
  info "state $STATE_FILE без живого контейнера — перезаписываю"
}

SUF="$(rnd 3)"                    # 6 hex — суффикс контейнера
DBSUF="$(rnd 6)"                  # 12 hex — суффикс БД (проходит stand_guard ^voice_tracker_t\w+_[0-9a-f]{6,}$)
CNAME="r2607_$SUF"
DB="voice_tracker_t07auth_$DBSUF"

# env-параметры скрипта (совпадают с конвенцией bootstrap_users): DB_USER_ROOT —
# имя root, DB_PASS_ROOT — его пароль; пароли пользователей плана — DB_USER_<USERNAME>.
ROOT_USER="${DB_USER_ROOT:-dsbot_root}"
case "$ROOT_USER" in
  *[!A-Za-z0-9_-]*|"") die "DB_USER_ROOT должен быть [A-Za-z0-9_-]+" ;;
esac
ROOT_PASS="${DB_PASS_ROOT:-$(rnd 12)}"
PW_APP="${DB_USER_DSBOT_APP:-$(rnd 12)}"
PW_WEB="${DB_USER_DSBOT_WEB:-$(rnd 12)}"
PW_MIG="${DB_USER_DSBOT_MIGRATION:-$(rnd 12)}"
PW_BKP="${DB_USER_DSBOT_BACKUP:-$(rnd 12)}"
PW_RST="${DB_USER_DSBOT_RESTORE:-$(rnd 12)}"
# пароли попадают в eval-строки URI и окружение helper'а — только hex
# (исключает кавычки/;/переводы строк из env; rand-значения проходят всегда)
for pw in "$ROOT_PASS" "$PW_APP" "$PW_WEB" "$PW_MIG" "$PW_BKP" "$PW_RST"; do
  case "$pw" in *[!0-9a-f]*) die "пароль из env должен быть hex [0-9a-f]+ (в eval-URI и env helper'а интерполируется только hex)" ;; esac
  [ "${#pw}" -ge 12 ] || die "пароль из env короче 12 символов"
done
# Имена переменных совпадают с конвенцией bootstrap_users/CLI `users --bootstrap`
# (DB_USER_ROOT/DB_PASS_ROOT + DB_USER_<USERNAME> для каждого пользователя
# USER_PLAN). Значения НЕ экспортируются в окружение скрипта: helper получает их
# по stdin (см. helper-фазу ниже) — `docker run -e KEY` на этом хосте их не
# видел бы (Windows docker.exe из WSL + WSL-export), а argv-вариант светил бы
# секреты в списке процессов.

info "docker run $IMAGE --auth (127.0.0.1:$PORT → 27017), контейнер $CNAME"
# без named volume: данные в anonymous volume — удаляются вместе с контейнером
# в --down (docker rm -v)
"$DOCKER_BIN" run -d --name "$CNAME" -p "127.0.0.1:${PORT}:27017" "$IMAGE" --auth >/dev/null \
  || die "docker run не удался (порт $PORT занят? тогда: $0 --down или освободить порт)"
printf '%s\n' "$CNAME" > "$STATE_FILE"
chmod 600 "$STATE_FILE" 2>/dev/null || true

# ждёт готовности mongod: ping не требует credentials — работает и до, и после
# создания пользователей
deadline=$((SECONDS + 120))
ready=0
while [ "$SECONDS" -lt "$deadline" ]; do
  if "$DOCKER_BIN" exec "$CNAME" mongosh --quiet --eval \
       'db.adminCommand({ping:1}).ok' 2>/dev/null | grep -q 1; then
    ready=1
    break
  fi
  sleep 2
done
[ "$ready" = 1 ] || { "$DOCKER_BIN" logs --tail 20 "$CNAME" >&2 || true; die "mongod не отвечает на ping за 120s"; }

# --- ТОЧНАЯ production-точка входа (review R26-07, blocker 2): root по
# ROOT_ROLE_PLAN и весь ROLE_PLAN/USER_PLAN создаёт ОДНА боевая команда
# `python -m voice_tracker.migrate users --bootstrap` (bootstrap_users ->
# localhost exception -> ensure_roles/ensure_users), а не самописный
# mongosh-генератор. localhost exception принимает соединения только с ЧЕСТНОГО
# loopback mongod (через published порт наружу она не проходит — живой прогон),
# поэтому helper-контейнер расшаривает сетевой namespace mongo
# (--network container:$CNAME) — прямой аналог mongo-bootstrap с
# network_mode: service:mongo в боевом compose. Исходники пакета передаются
# tar-ом через stdin (bind-mount не используется — не зависит от трансляции
# путей Windows/WSL), pymongo ставится pip'ом в эфемерную ФС helper'а, helper
# снимается по trap (в т.ч. при ошибке) и --rm — следов не оставляет. Версия
# pymongo — та же граница, что в pyproject.toml.
# Креды — отдельным `docker exec -i` по stdin в файл 0600 внутри контейнера
# (см. шапочку «Гигиена секретов»): ни -e KEY (Windows docker.exe из WSL не
# видит WSL-export — падение живого прогона 2026-09-27), ни -e KEY=value /
# аргументы в argv, ни файл на диске хоста. Файл кредов стирается ДО запуска
# python, так что после шага его нет ни на хосте, ни в контейнере.
# stdout helper'а (JSON имён созданных сущностей, без секретов) уводится в
# stderr: stdout стенда остаётся только eval-блок ниже.
HNAME="${CNAME}h"
cleanup_helper() {
  [ -n "${HNAME:-}" ] || return 0
  "$DOCKER_BIN" rm -f "$HNAME" >/dev/null 2>&1 || true
}
# rm по любому выходу из скрипта (ошибка die, сигнал, нормальное завершение);
# после успешной helper-фазы trap снимается явно ниже.
trap 'cleanup_helper' EXIT
trap 'cleanup_helper; exit 130' INT
trap 'cleanup_helper; exit 143' TERM
{
  # Для команды, стоящей СЛЕВА от `||`, bash отключает errexit: без явного
  # `|| die` на каждом шаге отказ шага не прервал бы группу, и следующий шаг
  # ушёл бы работать с битым состоянием helper'а. Отсюда — свой `|| die` у
  # каждого из четырёх шагов.
  # спит 900s: backstop на случай, если хост-процесс убит наповал (trap не
  # отработает) — helper завершится сам, а --rm удалит контейнер; на штатном
  # пути 15 минут с запасом покрывают pip install + bootstrap
  "$DOCKER_BIN" run -d --rm --name "$HNAME" --network "container:$CNAME" \
      "$PY_IMAGE" sh -c 'exec sleep 900' >/dev/null \
    || die "helper-контейнер $HNAME не поднялся (образ $PY_IMAGE есть локально?)"
  # (1) исходники пакета — tar-ом по stdin этого exec
  tar -C "$REPO" -cf - voice_tracker \
    | "$DOCKER_BIN" exec -i "$HNAME" sh -eu -c 'mkdir -p /app && tar -xf - -C /app' \
    || die "не удалось разложить voice_tracker в helper (tar | docker exec -i)"
  # (2) креды — по stdin этого exec (printf — builtin, в argv не светятся)
  printf '%s\n' \
    "DB_USER_ROOT=$ROOT_USER" "DB_PASS_ROOT=$ROOT_PASS" \
    "DB_USER_DSBOT_APP=$PW_APP" "DB_USER_DSBOT_WEB=$PW_WEB" \
    "DB_USER_DSBOT_MIGRATION=$PW_MIG" "DB_USER_DSBOT_BACKUP=$PW_BKP" \
    "DB_USER_DSBOT_RESTORE=$PW_RST" \
    | "$DOCKER_BIN" exec -i "$HNAME" sh -eu -c 'umask 077; cat > /run/r2607.creds' \
    || die "не удалось передать креды в helper по stdin (файл кредов не создан)"
  # (3) allexport из файла -> env только этого sh; файл стёрт ДО запуска python.
  #     Проверка «креды реально не пустые» — внутри helper'а: иначе пустой stdin
  #     на шаге (2) выглядел бы как «pypi недоступен/том пуст» (значение не
  #     печатается, только имя переменной).
  "$DOCKER_BIN" exec "$HNAME" sh -eu -c \
      'umask 077; set -a; . /run/r2607.creds; set +a; rm -f /run/r2607.creds; \
        if [ -z "${DB_USER_ROOT:-}" ] || [ -z "${DB_PASS_ROOT:-}" ]; then \
          echo "r2607: DB_USER_ROOT/DB_PASS_ROOT не дошли до helper (пустой поток кредов)" >&2; \
          exit 3; \
        fi; \
        pip install --quiet --no-cache-dir "pymongo>=4.6,<5" \
        && cd /app \
        && exec python -B -m voice_tracker.migrate users --bootstrap \
             --uri mongodb://127.0.0.1:27017/admin --db "$1"' _ "$DB" \
    || die "production users --bootstrap не удалось (см. вывод helper'а выше: pypi недоступен? том пуст? креды не дошли) — контейнер $CNAME остаётся, снимается $0 --down"
} >&2
cleanup_helper
trap - EXIT INT TERM
info "root (ROOT_ROLE_PLAN) и пользователи плана созданы production-путём users --bootstrap (БД $DB)"

# stdout — только eval-блок (никто не должен печатать в stdout до этих строк)
echo "export TEST_MONGO_PORT=$PORT"
echo "export TEST_MONGO_DB=$DB"
echo "export TEST_MONGO_ADMIN_URI='mongodb://$ROOT_USER:$ROOT_PASS@127.0.0.1:$PORT/admin?authSource=admin'"
echo "export TEST_MONGO_APP_URI='mongodb://dsbot_app:$PW_APP@127.0.0.1:$PORT/?authSource=$DB'"
echo "export TEST_MONGO_WEB_URI='mongodb://dsbot_web:$PW_WEB@127.0.0.1:$PORT/?authSource=$DB'"
echo "export TEST_MONGO_MIGRATION_URI='mongodb://dsbot_migration:$PW_MIG@127.0.0.1:$PORT/?authSource=$DB'"
# review R26-07, blocker 1: URI пользователей бэкап-плана создаются ВМЕСТЕ с
# самим планом (authSource=рабочая БД, НЕ admin) и ОБЯЗАТЕЛЬНО проверяются
# живым тестом аутентификации (tests/test_mongo_auth_stand.py).
echo "export TEST_MONGO_BACKUP_URI='mongodb://dsbot_backup:$PW_BKP@127.0.0.1:$PORT/?authSource=$DB'"
echo "export TEST_MONGO_RESTORE_URI='mongodb://dsbot_restore:$PW_RST@127.0.0.1:$PORT/?authSource=$DB'"
# localhost-URI без credentials: аргумент local_uri боевого bootstrap_users в
# тесте повторного идемпотентного прогона поверх живого кластера
echo "export TEST_MONGO_ROOT_LOCAL='mongodb://127.0.0.1:$PORT/admin'"
