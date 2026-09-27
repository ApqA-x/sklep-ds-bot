#!/usr/bin/env bash
# R26-07: ОДНОРАЗОВЫЙ auth-стенд для enforcement-проверки модели прав (DB06).
#
# Поднимает mongo:7 с --auth на 127.0.0.1:27098, проходит localhost exception
# (первый root на пустом томе) и создаёт роли/пользователей СТРОГО из плана
# voice_tracker/migrate.py (ROLE_PLAN/USER_PLAN) — генерация mongosh-скрипта
# импортом этого модуля, чтобы план не дублировался руками. Целевая БД —
# voice_tracker_t07auth_<hex> (одноразовая, под стендовый guard имён).
#
# usage:
#   eval "$(deploy/scripts/r2607_auth_stand.sh up)"   # stdout — только export'ы
#   python -B -m pytest tests/test_mongo_auth_stand.py -q -m integration
#   deploy/scripts/r2607_auth_stand.sh --down
#
# Гигиена секретов: пароли — случайный hex (или значения env-параметров
# DB_USER_ROOT/DB_PASS_ROOT/DB_USER_DSBOT_*), наружу выходят ТОЛЬКО export-
# строки в stdout для eval; в файлы ничего не пишется, кроме state-файла
# имени контейнера (chmod 600). Пароли видны в argv mongosh внутри контейнера
# — приемлемо для одноразового стенда на машине оператора, НЕ для прода.
#
# ВНИМАНИЕ: root стенда получает не только userAdminAnyDatabase (как в
# bootstrap_users), но и readWriteAnyDatabase/dbAdminAnyDatabase/backup/
# restore/clusterMonitor — иначе mongod запрещает granter'у выдавать роли,
# привилегии которых у него нет (createUser dsbot_backup с backup@admin).
set -euo pipefail
# shellcheck source=_common.sh
source "$(dirname "$0")/_common.sh"

DOCKER_BIN="${DOCKER_BIN:-/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe}"
IMAGE="${R2607_IMAGE:-mongo:7}"
PORT=27098                       # стендовый auth-порт; 27017/27018 (прод/dev) запрещены
STATE_FILE="${R2607_STATE:-/tmp/r2607_stand.name}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

rnd() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

usage_stand() {
  cat >&2 <<'EOF'
usage: r2607_auth_stand.sh [--down | -h]
  без аргументов — up: контейнер r2607_<hex> (mongo:7 --auth, 127.0.0.1:27098),
  localhost exception root, роли/пользователи из ROLE_PLAN/USER_PLAN;
  в stdout — export-строки URI для eval. --down — удалить контейнер из state.
EOF
}

have_docker() {
  [ -f "$DOCKER_BIN" ] || command -v "$DOCKER_BIN" >/dev/null 2>&1 \
    || die "docker not found: $DOCKER_BIN (переопределение: DOCKER_BIN=/path/to/docker)"
}

mongosh_in() { # mongosh_in [uri] — mongosh внутри контейнера
  local uri="${1:-}"
  if [ -n "$uri" ]; then
    "$DOCKER_BIN" exec "$CNAME" mongosh --quiet "$uri" --eval "$JS"
  else
    "$DOCKER_BIN" exec "$CNAME" mongosh --quiet --eval "$JS"
  fi
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
    "$DOCKER_BIN" rm -f "$name" >/dev/null || die "docker rm -f $name не удалось"
    rm -f "$STATE_FILE"
    info "auth стенд снят: $name"
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
# пароли интерполируются в JS-литералы — только hex (исключает кавычки/;break-
# символы из env; rand-значения проходят всегда)
for pw in "$ROOT_PASS" "$PW_APP" "$PW_WEB" "$PW_MIG" "$PW_BKP" "$PW_RST"; do
  case "$pw" in *[!0-9a-f]*) die "пароль из env должен быть hex [0-9a-f]+ (в JS-литералы интерполируется только hex)" ;; esac
  [ "${#pw}" -ge 12 ] || die "пароль из env короче 12 символов"
done
export DB_USER_DSBOT_APP="$PW_APP" DB_USER_DSBOT_WEB="$PW_WEB" \
       DB_USER_DSBOT_MIGRATION="$PW_MIG" DB_USER_DSBOT_BACKUP="$PW_BKP" \
       DB_USER_DSBOT_RESTORE="$PW_RST"

info "docker run $IMAGE --auth (127.0.0.1:$PORT → 27017), контейнер $CNAME"
# без named volume: данные в anonymous volume — удаляются вместе с контейнером
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

# --- localhost exception: первый root на пустом томе (только с 127.0.0.1
# внутри контейнера — наружу через published порт exception не проходит) ----
JS="
if (db.getSiblingDB('admin').runCommand({createUser: '$ROOT_USER', pwd: '$ROOT_PASS', roles: [
     {role: 'userAdminAnyDatabase', db: 'admin'},
     {role: 'readWriteAnyDatabase', db: 'admin'},
     {role: 'dbAdminAnyDatabase', db: 'admin'},
     {role: 'backup', db: 'admin'},
     {role: 'restore', db: 'admin'},
     {role: 'clusterMonitor', db: 'admin'}]}).ok !== 1)
  throw new Error('r2607: root create failed');
"
mongosh_in >/dev/null \
  || die "localhost exception не сработала (том не пуст? повторный up без --down) — контейнер $CNAME остаётся, снимается $0 --down"
info "root создан через localhost exception"

# --- роли/пользователи: mongosh-скрипт генерируется из ROLE_PLAN/USER_PLAN --
PY_BIN=""
for c in python3 python; do
  command -v "$c" >/dev/null 2>&1 && { PY_BIN="$c"; break; }
done
[ -n "$PY_BIN" ] || die "python3 не найден на хосте — нужен для генерации скрипта из ROLE_PLAN"

users_js="$(R2607_REPO="$REPO" "$PY_BIN" -B - "$DB" <<'PY'
import json
import os
import sys

sys.path.insert(0, os.environ["R2607_REPO"])
from voice_tracker.migrate import ROLE_PLAN, USER_PLAN

dbname = sys.argv[1]

# runCommand не бросает исключение при отказе — возвращает {ok:0, codeName};
# без проверки mongosh вышел бы с нулём. В сообщение попадает только имя шага
# и код — ни pwd, ни полный документ команды.
HELPER = ("function r(res, what) { if (res.ok !== 1) { throw new Error("
          "'r2607: ' + what + ' failed: ' + String(res.codeName || res.code)); } return res; }")

def stmt(cmd, what):
    return "r(db.getSiblingDB(%s).runCommand(%s), %s);" % (
        json.dumps(dbname), json.dumps(cmd), json.dumps(what))

# порядок: сначала кастомные роли (их выдаёт createUser), затем пользователи
lines = [HELPER]
for role, actions in ROLE_PLAN.items():
    lines.append(stmt({
        "createRole": role,
        "privileges": [{"resource": {"db": dbname, "collection": ""}, "actions": sorted(actions)}],
        "roles": [],
    }, "createRole " + role))
for username, roles, _note in USER_PLAN:
    pwd = os.environ.get("DB_USER_%s" % username.upper())
    if not pwd:
        raise SystemExit("DB_USER_%s not set" % username.upper())
    desired = [{"role": r, "db": dbname if d == "{dbname}" else d} for r, d in roles]
    lines.append(stmt({"createUser": username, "pwd": pwd, "roles": desired},
                      "createUser " + username))
print("\n".join(lines))
PY
)" || die "не удалось сгенерировать mongosh-скрипт из ROLE_PLAN/USER_PLAN"

JS="$users_js"
mongosh_in "mongodb://$ROOT_USER:$ROOT_PASS@127.0.0.1:27017/admin?authSource=admin" >/dev/null \
  || { "$DOCKER_BIN" logs --tail 5 "$CNAME" >&2 || true; die "создание ролей/пользователей плана не удалось — контейнер $CNAME остаётся, снимается $0 --down"; }
info "роли и пользователи плана созданы (БД $DB)"

# stdout — только eval-блок (никто не должен печатать в stdout до этих строк)
echo "export TEST_MONGO_PORT=$PORT"
echo "export TEST_MONGO_DB=$DB"
echo "export TEST_MONGO_ADMIN_URI='mongodb://$ROOT_USER:$ROOT_PASS@127.0.0.1:$PORT/admin?authSource=admin'"
echo "export TEST_MONGO_APP_URI='mongodb://dsbot_app:$PW_APP@127.0.0.1:$PORT/?authSource=$DB'"
echo "export TEST_MONGO_WEB_URI='mongodb://dsbot_web:$PW_WEB@127.0.0.1:$PORT/?authSource=$DB'"
echo "export TEST_MONGO_MIGRATION_URI='mongodb://dsbot_migration:$PW_MIG@127.0.0.1:$PORT/?authSource=$DB'"
echo "export TEST_MONGO_ROOT_LOCAL='mongodb://127.0.0.1:$PORT/admin'"
