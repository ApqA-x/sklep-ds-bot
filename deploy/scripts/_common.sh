#!/usr/bin/env bash
# Общие помощники deploy-скриптов (T13). Никакого implicit cwd:
# Пути вычисляются от расположения этого файла; project/file/env задаются явно.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "ERROR: $*" >&2; exit 2; }
info() { echo "deploy: $*" >&2; }

usage_env_arg() {
  cat >&2 <<'EOF'
usage: script <production|staging> [additional args]
  Явное окружение обязательно — проект/файл/env-путь выводятся из него,
  из случайного cwd ничего не запускается (п.10 плана T13).
EOF
}

resolve_env() {
  case "${1:-}" in
    production)
      PROFILE=production
      PROJECT=dsbot-prod
      COMPOSE_FILE="$DEPLOY_DIR/production/compose.yml"
      ENV_FILE="${DSBOT_ENV_FILE:-$DEPLOY_DIR/production/.env}"
      ;;
    staging)
      PROFILE=staging
      PROJECT=dsbot-staging
      COMPOSE_FILE="$DEPLOY_DIR/staging/compose.staging.yml"
      ENV_FILE="${DSBOT_ENV_FILE:-$DEPLOY_DIR/staging/.env}"
      ;;
    *) usage_env_arg; exit 2 ;;
  esac
  [ -f "$COMPOSE_FILE" ] || die "compose file missing: $COMPOSE_FILE"
}

compose() {
  # R26-06/V26-16: единый источник env. Интерполяция ${DSBOT_ENV_FILE} в YAML
  # (секция env_file) и --env-file получают ОДИН И ТОТ ЖЕ путь. Переменная
  # экспортируется только на время этой команды — окружение дочерних скриптов
  # не отравляется. DSBOT_ENV_FILE НЕ должен быть определён внутри env-файла
  # (validate_env.py это отвергает).
  DSBOT_ENV_FILE="$ENV_FILE" docker compose -p "$PROJECT" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker not installed on this host"
  docker compose version >/dev/null 2>&1 || die "docker compose v2 plugin missing"
  docker info >/dev/null 2>&1 || die "docker daemon is not reachable"
}

# имена сервисов без запуска (dry-вывод списка до операции)
list_services() {
  compose config --services | sort
}
