#!/usr/bin/env bash
# R26-07: ОДНОРАЗОВЫЙ auth-стенд для enforcement-проверки модели прав (DB06).
#
# Поднимает mongo:7 с --auth на 127.0.0.1:27098 и прогоняет по чистому кластеру
# ТОЧНУЮ ПРОДАКШН-ТОЧКУ ВХОДА начальных прав: `python -m
# voice_tracker.migrate users --bootstrap` (voice_tracker.migrate.bootstrap_users
# -> ROOT_ROLE_PLAN/ROLE_PLAN/USER_PLAN), а СРАЗУ ПОСЛЕ неё — deploy-equivalent
# путь runner'а: `python -m voice_tracker.migrate up` и `status` под
# dsbot_migration-URI (тот же вход, что compose-сервис schema-migrate: URI и БД
# только из окружения контейнера). helper-контейнер (python:3.12-slim,
# pymongo ставится pip'ом в эфемерную ФС) расшаривает сетевой namespace mongo
# (--network container:$CNAME), поэтому 127.0.0.1:27017 внутри него — ЧЕСТНЫЙ
# loopback mongod и localhost exception срабатывает ровно так же, как на проде
# (review R26-07, blocker 2: состав root обязан доказываться production-путём,
# а не самописным mongosh-генератором, который мог «спрятать» расхождение).
# Целевая БД — voice_tracker_t07auth_<hex> (одноразовая, под стендовый guard
# имён). Скрипта ролей из-под хоста больше нет: plan создаёт production-код.
#
# Миграционная фаза (review R26-07, blocker 2) делает ровно то, что на проде
# делает `--profile migrate run --rm schema-migrate`, и доказывает это на живом
# --auth сервере: (1) вставляет legacy-документ guild_settings БЕЗ revision
# допустимым записывающим путём (insert под dsbot_migration); (2) гоняет
# `migrate up` под dsbot_migration-URI — весь план M1–M7 обязан пройти РОВНО под
# ROLE_PLAN: M1/M3–M6 падают с code 13 без createIndex/createCollection (у
# runtime-роли их нет, и на без-auth стенде 27099 это непроверяемо в принципе),
# а читающая часть шагов — серверный aggregate в _dup_groups (M3/M4) и
# count_documents (M7) — авторизуется тем же `find` (отдельного action
# `aggregate` в Mongo нет, createRole его отвергает); (3) `migrate status`
# (rc=0, backfill-шаг done, latest==SCHEMA_VERSION); (4) сверяет с сервером, что
# legacy-документ получил revision==0 (M7 runner backfill); (5) ОБЯЗАТЕЛЬНО
# отвергает безпарольный CLI `migrate status --uri mongodb://127.0.0.1:27017/...`
# с rc!=0 и кодом Unauthorized (localhost exception после bootstrap не даёт
# ничего). Наружу — только whitelist маркеров R2607_* без секретов (ниже).
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
# эфемерной ФС контейнера; файл читается шагом bootstrap и ШАГОМ МИГРАЦИЙ
# (каждый своей порцией: миграционной фазе нужен ровно один ключ — пароль
# dsbot_migration) и стирается ДО запуска python в обеих фазах, а в любом
# случае исчезает вместе с helper'ом (явный rm по trap + --rm + ограниченный
# sleep). Сознательно НЕ используются:
# (а) `docker run -e KEY` — Windows docker.exe из WSL не получает WSL-export по
# имени (падение живого прогона 2026-09-27: «users --bootstrap требует
# DB_USER_ROOT и DB_PASS_ROOT»), (б) `-e KEY=value` и positional-аргументы —
# секреты в argv, (в) env-файл на диске хоста — его docker.exe не прочитал бы
# по WSL-пути (та же причина, по которой здесь нет bind-mount), а любой файл на
# диске оставляет секреты при ошибке. Миграционная фаза собирает MONGO_URI
# (dsbot_migration) из переменных ВНУТРИ контейнера — значения не попадают ни в
# argv хоста, ни в stdout; в env дочерних CLI-прогонов plan-переменные
# DB_USER_*/DB_PASS_* не пробрасываются вообще (только MONGO_URI/MONGO_DB).
# Наружу из helper'а выпускаются ТОЛЬКО именованные маркеры миграционной фазы
# (R2607_*): bash сверяет имя по whitelist, значение — по жёсткому набору
# символов (нет '@', '/', пробелов, кавычек) и дополнительно отвергает любое
# значение, совпадающее с одним из паролей, после чего проверяет сами значения
# (rc=0 у up/status, done у backfill-шага, rc!=0 у безпарольного отказа).
# Из stdout helper'а (JSON имён созданных сущностей и отчётов migrate) ничего
# не печатается в stdout стенда — вывод фаз идёт в stderr, а stdout
# миграционной фазы сначала целиком валидируется и только потом попадает в
# eval-блок.
# На диск хоста пишется лишь state-файл имени контейнера (chmod 600, без
# секретов).
#
# Уборка: --down удаляет контейнер вместе с anonymous volume (-v), т.е. БЕЗ
# остатка данных, и заодно leftover helper-контейнер `${CNAME}h` от прерванного
# up (best-effort). Сам helper снимается в том же up: trap по EXIT/INT/TERM +
# --rm, поэтому креды не переживают завершение скрипта; если хост-процесс убит
# наповал (trap не отработает) — helper завершается сам по ограниченному sleep
# 1800s, и креды исчезают вместе с ним;
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
  users --bootstrap` в helper-контейнере с network namespace mongod; следом
  тем же helper'ом прогоняется deploy-equivalent путь runner'а (`migrate up` и
  `migrate status` под dsbot_migration-URI из окружения, плюс обязательный
  отказ безпарольного CLI).
  В stdout — export-строки URI для eval и несекретные признаки прогона
  (маркеры R2607_*). --down — удалить контейнер и его anonymous volume из state.
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
  # спит 1800s: backstop на случай, если хост-процесс убит наповал (trap не
  # отработает) — helper завершится сам, а --rm удалит контейнер; на штатном
  # пути 30 минут с запасом покрывают pip install + bootstrap + миграционную
  # фазу (up/status/отказ безпарольного CLI)
  "$DOCKER_BIN" run -d --rm --name "$HNAME" --network "container:$CNAME" \
      "$PY_IMAGE" sh -c 'exec sleep 1800' >/dev/null \
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
info "root (ROOT_ROLE_PLAN) и пользователи плана созданы production-путём users --bootstrap (БД $DB)"

# ------------------------------------------------------ миграционная фаза
# review R26-07, blocker 2: deploy-equivalent путь runner'а обязан быть доказан
# ЭТИМ ЖЕ кластером и ЭТИМ ЖЕ helper'ом. На проде `migrate up`/`status` — это
# compose-сервис schema-migrate с MONGO_URI=${MONGO_MIGRATION_URI} и MONGO_DB из
# env-файла (profile `migrate`); здесь тот же вход повторяется буквально: URI и
# БД уходят в CLI ТОЛЬКО окружением дочернего процесса (никаких --uri с
# credentials в argv), plan-переменные кредов в это окружение не пробрасываются.
# Программа фазы выкладывается отдельным `docker exec -i` (её текст секретов не
# содержит) и печатает в stdout ИСКЛЮЧИТЕЛЬНО маркеры `R2607_<KEY>=<value>`;
# stdout CLI-команд она перехватывает и наружу не отдаёт. Маркеры валидируются
# bash (whitelist имён + жёсткий набор символов значения + сверка ни одно
# значение не равно одному из паролей) и проверяются по существу — стенд обязан
# упасть здесь, а не отдать pytest «зелёный» прогон без живой проверки.
R2607_MIG_KEYS="R2607_MIGRATE_UP_RC R2607_MIGRATE_STATUS_RC R2607_MIGRATE_UP_APPLIED R2607_SCHEMA_LATEST R2607_SCHEMA_VERSION R2607_BACKFILL_STEP R2607_BACKFILL_STATUS R2607_BACKFILL_PENDING R2607_BACKFILL_DONE R2607_LEGACY_ID R2607_LEGACY_REVISION R2607_PASSWORDLESS_RC R2607_PASSWORDLESS_DENIED"
{
  "$DOCKER_BIN" exec -i "$HNAME" sh -eu -c 'cat > /run/r2607_migrate_phase.py' <<'R2607PYEOF' || die "не удалось выложить миграционную фазу в helper (docker exec -i < script)"
# R2607: миграционная фаза auth-стенда — deploy-equivalent прогон runner'а под
# dsbot_migration (см. шапку deploy/scripts/r2607_auth_stand.sh).
#
# Контракт вывода: в stdout — только строки `R2607_<KEY>=<value>` из whitelist
# стендового скрипта (значение без '@', '/', пробелов и кавычек); diagnostics —
# в stderr. Секрет (пароль dsbot_migration) не печатается ни в одном из потоков:
# stdout CLI-прогонов перехватывается, а diagnostics проходят через scrub().
import json
import os
import re
import subprocess
import sys
import uuid

DB = sys.argv[1] if len(sys.argv) > 1 else ""
if not re.fullmatch(r"voice_tracker_t\w+_[0-9a-f]{6,}", DB):
    sys.stderr.write("r2607/migrate: имя БД вне стендового шаблона — выход без подключения\n")
    raise SystemExit(4)

PW_MIG = os.environ.get("DB_USER_DSBOT_MIGRATION", "")
if not re.fullmatch(r"[0-9a-f]{12,}", PW_MIG):
    sys.stderr.write("r2607/migrate: DB_USER_DSBOT_MIGRATION не дошёл до helper-фазы "
                     "(пустой поток кредов?)\n")
    raise SystemExit(3)

sys.path.insert(0, "/app")
try:
    import pymongo
    from voice_tracker import migrate, schema
except Exception as exc:
    sys.stderr.write(f"r2607/migrate: helper не готов ({type(exc).__name__}: {exc}) — "
                     "pip install/pymongo отработал в bootstrap-фазе?\n")
    raise SystemExit(5) from None

MIG_URI = f"mongodb://dsbot_migration:{PW_MIG}@127.0.0.1:27017/?authSource={DB}"
# безпарольный честный loopback: ровно то подключение, которым localhost
# exception пользуется ДО bootstrap; после bootstrap оно обязано быть отвергнуто
LOCAL_URI = "mongodb://127.0.0.1:27017/admin"
LEGACY_ID = "t07legacy_" + uuid.uuid4().hex[:12]
BACKFILL_ID = next(m.id for m in migrate.MIGRATIONS if m.name == "guild-settings-revision-backfill")

# env дочерних CLI-прогонов: только MONGO_DB (+ MONGO_URI там, где он нужен).
# Креды plan-переменных (DB_USER_*/DB_PASS_*) в детское окружение НЕ уходят.
BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith("DB_")}
BASE_ENV.pop("MONGO_URI", None)
BASE_ENV["MONGO_DB"] = DB
MIG_ENV = dict(BASE_ENV, MONGO_URI=MIG_URI)


def scrub(text):
    return (text or "").replace(PW_MIG, "***")


def die(step, detail):
    sys.stderr.write(f"r2607/migrate: шаг «{step}» не прошёл: {scrub(detail)[-2500:]}\n")
    raise SystemExit(2)


def run_cli(args, env):
    """Точка входа deploy-прогона: `python -m voice_tracker.migrate <args>`.
    credentials — только в env (MONGO_URI), в argv их нет."""
    return subprocess.run([sys.executable, "-B", "-m", "voice_tracker.migrate", *args],
                          cwd="/app", env=env, capture_output=True, text=True, timeout=900)


def run_ok(args):
    proc = run_cli(args, MIG_ENV)
    if proc.returncode != 0:
        die(f"migrate {args[0]}", f"rc={proc.returncode} stderr:\n{proc.stderr}")
    try:
        return proc, json.loads(proc.stdout)
    except ValueError:
        die(f"migrate {args[0]}", f"CLI не вывел JSON: {scrub(proc.stdout)[:400]}")


def read_db():
    client = pymongo.MongoClient(MIG_URI, serverSelectionTimeoutMS=5000)
    return client, client[DB]


# (1) legacy-документ guild_settings БЕЗ revision — допустимым для стенда
# записывающим путём: insert под dsbot_migration (это данные, не DDL; у
# runtime-роли нет createCollection, нужного для implicit-create новой
# коллекции). Именно такой документ до R26-07 чинил startup рантайма — теперь
# его обязано поднять `migrate up`.
try:
    client, db = read_db()
    try:
        db[migrate.GUILD_SETTINGS].insert_one(
            {"_id": LEGACY_ID, "guildId": "1", "r2607": "legacy-without-revision"})
    finally:
        client.close()
except Exception as exc:
    die("legacy insert", f"{type(exc).__name__}: {exc}")

# (2) deploy-equivalent `migrate up` под dsbot_migration
up, up_doc = run_ok(["up"])
actions = up_doc.get("actions") or []
got = [a.get("action") for a in actions]
want = ["applied"] * len(migrate.MIGRATIONS)
if got != want:
    die("migrate up", "на чистом стенде ожидались применёнными ВСЕ шаги плана "
                      f"{want}, получено {json.dumps(actions, default=str)[:800]}")

# (3) deploy-equivalent `migrate status` под dsbot_migration
#     contract CLI: {"latest": <int version из schema_versions>,
#                    "migrations": {"<id>": {..., "status", "report"}},
#                    "lock": ...} — ключи migrations после json.dumps СТРОКИ.
status, st = run_ok(["status"])
migrations = st.get("migrations") or {}
if str(BACKFILL_ID) not in migrations:
    die("migrate status", f"status.migrations не содержит M{BACKFILL_ID}: "
                          f"ключи {sorted(migrations)}")
step_doc = migrations[str(BACKFILL_ID)]
if step_doc.get("status") != "done":
    die("migrate status", f"M{BACKFILL_ID} не done: {json.dumps(step_doc, default=str)[:600]}")
report = step_doc.get("report") or {}
pending, backfilled = int(report.get("pending") or 0), int(report.get("backfilled") or 0)
if backfilled < 1:
    die("migrate status", f"M{BACKFILL_ID} не забэкафил ни одного документа "
                          f"(legacy-док в выборку не попал): {json.dumps(report, default=str)[:400]}")
latest_raw = st.get("latest")
# `or -1` съел бы законный latest=0 (пустая schema_versions — ровно тот случай,
# когда up ничего не зафиксировал); сверяем с SCHEMA_VERSION — не с «любым» числом
latest = int(latest_raw) if isinstance(latest_raw, int) else -1
if latest != schema.SCHEMA_VERSION:
    die("migrate status", f"latest={latest} != SCHEMA_VERSION={schema.SCHEMA_VERSION}")

# (4) proof с сервера: у legacy-документа revision == 0 (не None, не 1)
try:
    client, db = read_db()
    try:
        legacy = db[migrate.GUILD_SETTINGS].find_one({"_id": LEGACY_ID})
    finally:
        client.close()
except Exception as exc:
    die("legacy readback", f"{type(exc).__name__}: {exc}")
if not legacy or legacy.get("revision") != 0:
    die("legacy readback", f"после up документ без revision обязан получить revision=0, "
                          f"получено: {json.dumps(legacy, default=str)[:400]}")

# (5) безпарольный CLI после bootstrap обязан быть отвергнут (rc != 0) именно
# отказом авторизации, а не таймаутом/отсутствием пакета: иначе «доказательство
# auth» было бы ложным.
neg = run_cli(["status", "--uri", LOCAL_URI, "--db", DB], BASE_ENV)
blob = f"{neg.stdout}\n{neg.stderr}"
if neg.returncode == 0:
    die("passwordless CLI", "безпарольный `migrate status` ПРОШЕЛ (rc=0) — на --auth "
                           "кластере после bootstrap это дыра в enforcement")
denied = "unauthorized" if re.search(
    r"not authorized|Unauthorized|code: 13|requires auth|AuthenticationRequired", blob, re.I) else "other"
if denied != "unauthorized":
    die("passwordless CLI", f"отказ не по причине авторизации (rc={neg.returncode}): {blob[-800:]}")
print(f"R2607_MIGRATE_UP_RC={up.returncode}")
print(f"R2607_MIGRATE_STATUS_RC={status.returncode}")
print(f"R2607_MIGRATE_UP_APPLIED={len(actions)}")
print(f"R2607_SCHEMA_LATEST={latest}")
print(f"R2607_SCHEMA_VERSION={schema.SCHEMA_VERSION}")
print(f"R2607_BACKFILL_STEP=M{BACKFILL_ID}")
print(f"R2607_BACKFILL_STATUS={step_doc.get('status')}")
print(f"R2607_BACKFILL_PENDING={pending}")
print(f"R2607_BACKFILL_DONE={backfilled}")
print(f"R2607_LEGACY_ID={LEGACY_ID}")
print(f"R2607_LEGACY_REVISION={legacy.get('revision')}")
print(f"R2607_PASSWORDLESS_RC={neg.returncode}")
print(f"R2607_PASSWORDLESS_DENIED={denied}")
R2607PYEOF
  # миграционной фазе нужен РОВНО ОДИН пароль — остальные plan-креды ей не требуются
  printf '%s\n' "DB_USER_DSBOT_MIGRATION=$PW_MIG" \
    | "$DOCKER_BIN" exec -i "$HNAME" sh -eu -c 'umask 077; cat > /run/r2607.creds' \
    || die "не удалось передать migration-кред в helper по stdin"
} >&2

# кред в helper'е читается и сразу стирается: к моменту старта python файла с
# паролем нет ни на хосте, ни в контейнере (в окружении шага остаётся только
# DB_USER_DSBOT_MIGRATION, из которой программа фазы собирает MONGO_URI для env
# дочерних CLI-прогонов). PYTHONPATH не нужен: программа сама кладёт /app в
# sys.path и передаёт cwd=/app дочерним процессам.
if ! R2607_MIG_OUT="$("$DOCKER_BIN" exec "$HNAME" sh -eu -c \
      'umask 077; set -a; . /run/r2607.creds; set +a; rm -f /run/r2607.creds; \
        exec python -B /run/r2607_migrate_phase.py "$1"' _ "$DB")"; then
  die "deploy-equivalent прогон migrate up/status под dsbot_migration не прошёл \
(см. вывод helper'а выше) — контейнер $CNAME остаётся, снимается $0 --down"
fi
# docker.exe на Windows может притащить CRLF; маркерный парсер обязан видеть
# ровно те байты, которые напечатала программа фазы
R2607_MIG_OUT="${R2607_MIG_OUT//$'\r'/}"

# --- валидация маркеров: только whitelist-имена, только безопасные символы в
# значениях, никаких совпадений с паролями. Всё, что не прошло, — die (секреты
# в stdout не уходят даже при отказе).
V_UP_RC="" V_STATUS_RC="" V_APPLIED="" V_LATEST="" V_SV="" V_STEP="" V_STATUS="" \
V_PENDING="" V_BACKFILLED="" V_LEGACY_ID="" V_LEGACY_REV="" V_NEG_RC="" V_NEG_KIND=""
R2607_EXPORTS=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  case "$line" in
    R2607_[A-Z0-9_]*=*) : ;;
    *) die "миграционная фаза вернула строку вне маркерного формата — наружу не выпускаем" ;;
  esac
  key="${line%%=*}"
  val="${line#*=}"
  case " $R2607_MIG_KEYS " in
    *" $key "*) : ;;
    *) die "миграционная фаза вернула неизвестный маркер $key" ;;
  esac
  case "$val" in
    "") die "маркер $key пуст" ;;
    *[!A-Za-z0-9_.+-]*) die "маркер $key: значение вне безопасного набора символов" ;;
  esac
  for pw in "$ROOT_PASS" "$PW_APP" "$PW_WEB" "$PW_MIG" "$PW_BKP" "$PW_RST"; do
    case "$val" in *"$pw"*) die "маркер $key совпадает с паролем — наружу не выпускаем" ;; esac
  done
  case "$key" in
    R2607_MIGRATE_UP_RC) V_UP_RC="$val" ;;
    R2607_MIGRATE_STATUS_RC) V_STATUS_RC="$val" ;;
    R2607_MIGRATE_UP_APPLIED) V_APPLIED="$val" ;;
    R2607_SCHEMA_LATEST) V_LATEST="$val" ;;
    R2607_SCHEMA_VERSION) V_SV="$val" ;;
    R2607_BACKFILL_STEP) V_STEP="$val" ;;
    R2607_BACKFILL_STATUS) V_STATUS="$val" ;;
    R2607_BACKFILL_PENDING) V_PENDING="$val" ;;
    R2607_BACKFILL_DONE) V_BACKFILLED="$val" ;;
    R2607_LEGACY_ID) V_LEGACY_ID="$val" ;;
    R2607_LEGACY_REVISION) V_LEGACY_REV="$val" ;;
    R2607_PASSWORDLESS_RC) V_NEG_RC="$val" ;;
    R2607_PASSWORDLESS_DENIED) V_NEG_KIND="$val" ;;
  esac
  R2607_EXPORTS="${R2607_EXPORTS}export ${key}='${val}'
"
done <<R2607MARKERS
$R2607_MIG_OUT
R2607MARKERS

for key in $R2607_MIG_KEYS; do
  case "$R2607_MIG_OUT" in
    *"$key="*) : ;;
    *) die "миграционная фаза не вернула обязательный маркер $key" ;;
  esac
done

# гейты по существу: стенд обязан упасть, если живой контракт не сошёлся
# (кавычки внутри сообщений — одинарные: в двойных bash увидел бы `cmd`
# как подстановку команды и исполнил её на хосте)
[ "$V_UP_RC" = "0" ] || die "deploy-equivalent 'migrate up' под dsbot_migration завершился с rc=$V_UP_RC"
[ "$V_STATUS_RC" = "0" ] || die "deploy-equivalent 'migrate status' под dsbot_migration завершился с rc=$V_STATUS_RC"
case "$V_STEP" in
  M[1-9]*) : ;;
  *) die "миграционная фаза вернула неизвестный backfill-шаг $V_STEP" ;;
esac
[ "$V_STATUS" = "done" ] || die "$V_STEP не done на живом --auth сервере (статус $V_STATUS)"
[ "$V_BACKFILLED" -ge 1 ] 2>/dev/null || die "$V_STEP не забэкафил ни одного документа ($V_BACKFILLED)"
[ "$V_LEGACY_REV" = "0" ] || die "legacy guild_settings не получил revision=0 (получено $V_LEGACY_REV)"
[ "$V_LATEST" = "$V_SV" ] || die "status: latest=$V_LATEST != SCHEMA_VERSION=$V_SV"
case "$V_LEGACY_ID" in
  t07legacy_*) : ;;
  *) die "стендовый legacy-документ имеет чужой идентификатор $V_LEGACY_ID" ;;
esac
[ -n "$V_NEG_RC" ] || die "нет rc безпарольного прогона"
[ "$V_NEG_RC" != "0" ] || die "безпарольный 'migrate status' приняли (rc=0) — auth не отработал"
[ "$V_NEG_KIND" = "unauthorized" ] || die "безпарольный CLI отвергнут не авторизацией ($V_NEG_KIND)"

cleanup_helper
trap - EXIT INT TERM
info "deploy-equivalent путь доказан на живом --auth: migrate up/status под dsbot_migration, \
$V_STEP backfill=$V_BACKFILLED (revision legacy-дока = $V_LEGACY_REV), безпарольный CLI отвергнут (rc=$V_NEG_RC)"

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
# Признаки deploy-equivalent прогона (маркеры R2607_*): rc'ы CLI, статус
# backfill-шага, revision legacy-документа, rc/причина отказа
# безпарольного URI. Секретов здесь нет по построению: каждое значение прошло
# whitelist имён, жёсткий набор символов, сверку с паролями и гейты по существу
# выше; печатаются строго последними, чтобы stdout стенда оставался eval-блоком.
printf '%s' "$R2607_EXPORTS"
