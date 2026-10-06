#!/usr/bin/env bash
# T13 deploy: preflight → pull → up -d. Всегда dry-run по умолчанию.
#   deploy/scripts/deploy.sh <production|staging>          # план (ничего не трогает)
#   deploy/scripts/deploy.sh <production|staging> --apply  # реально применить
# R26-06/V26-15: раньше флаг сравнивался неверно (APPLY=1 против "--apply") и
# ветка применения была недостижима — любой запуск был dry-run с exit 0. Теперь
# --apply действительно выполняет pull → up → status; без флага — план и exit 0.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
resolve_env "${1:-}"
shift
APPLY=""
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    *) die "лишние аргументы: $a" ;;
  esac
done
[ -f "$ENV_FILE" ] || die "env file missing: $ENV_FILE"
[ -n "$APPLY" ] || echo "DRY-RUN (ничего не меняется). Добавьте --apply чтобы применить." >&2

bash "$DEPLOY_DIR/scripts/preflight.sh" "$PROFILE"

info "plan: project=$PROJECT services:"
list_services | sed 's/^/  - /'
if [ -z "$APPLY" ]; then
  info "dry-run завершён (image'ы НЕ скачаны, контейнеры НЕ созданы)"
  exit 0
fi

info "pulling pinned images"
compose pull --quiet
info "starting"
compose up -d --wait --wait-timeout 120 --remove-orphans
bash "$DEPLOY_DIR/scripts/status.sh" "$PROFILE"
info "DONE"
