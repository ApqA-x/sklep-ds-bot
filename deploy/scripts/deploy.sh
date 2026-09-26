#!/usr/bin/env bash
# T13 deploy: preflight → pull → up -d. Всегда dry-run по умолчанию.
#   deploy/scripts/deploy.sh <production|staging>          # план (ничего не трогает)
#   deploy/scripts/deploy.sh <production|staging> --apply  # реально применить
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
[ -n "$APPLY" ] || echo "DRY-RUN (ничего не меняется). Добавьте --apply чтобы применить." >&2

bash "$DEPLOY_DIR/scripts/preflight.sh" "$PROFILE"

info "plan: project=$PROJECT services:"
list_services | sed 's/^/  - /'
if [ "$APPLY" != "--apply" ]; then
  info "dry-run завершён (image'ы НЕ скачаны, контейнеры НЕ созданы)"
  exit 0
fi

info "pulling pinned images"
compose pull --quiet
info "starting"
compose up -d --remove-orphans
bash "$DEPLOY_DIR/scripts/status.sh" "$PROFILE"
info "DONE"
