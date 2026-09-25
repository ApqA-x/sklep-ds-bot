#!/usr/bin/env bash
# T13 status: что запущено, готово ли, где смотреть логи. Только чтение.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
resolve_env "${1:-}"
require_docker

compose ps
echo
info "readiness web:"
PORT="$(grep -Eo '^WEB_HOST_PORT=.*' "$ENV_FILE" 2>/dev/null | cut -d= -f2 || true)"
PORT="${PORT:-8000}"
if command -v curl >/dev/null 2>&1; then
  curl -fsS -m 5 "http://127.0.0.1:${PORT}/api/readyz" && echo || info "web /api/readyz недоступен (стартует или unhealthy)"
else
  docker run --rm curlimages/curl:8.9.1 -fsS -m 5 "http://host.docker.internal:${PORT}/api/readyz" || info "web /api/readyz недоступен"
fi
echo
info "heartbeat ботовых сервисов (признаки жизни; см. docs/runbook-health.md):"
for s in gateway tracker writer commands activity stalker; do
  docker compose -p "$PROJECT" exec -T "$s" python -m voice_tracker.healthcheck --service "$s" 2>&1 | sed "s/^/  [$s] /" || true
done
BACKUP_DIR="$(sed -n 's/^BACKUP_DIR=//p' "$ENV_FILE" 2>/dev/null | head -1 || true)"
if [ -n "$BACKUP_DIR" ]; then
  echo
  info "свежесть точки восстановления (T14 B06):"
  "$DEPLOY_DIR/backup/backup_status.sh" "$PROFILE" || info "см. docs/runbook-backup.md"
fi
echo
info "логи: docker compose -p $PROJECT -f $COMPOSE_FILE logs --since 30m [--tail 200] <service>"
