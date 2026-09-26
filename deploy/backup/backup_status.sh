#!/usr/bin/env bash
# B06: возраст последней проверенной точки восстановления против лимита
# (BACKUP_MAX_AGE_HOURS в .env профиля, по умолчанию 26h = RPO 24h + grace).
# Только чтение: ни age, ни passphrase для этого не нужны.
# exit 1 = оператору видно (systemd OnFailure / cron-алерт / ручной запуск).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/_common.sh
source "$HERE/../scripts/_common.sh"

resolve_env "${1:-}"
[ -f "$ENV_FILE" ] || die "env file missing: $ENV_FILE"
BACKUP_DIR="$(sed -n 's/^BACKUP_DIR=//p' "$ENV_FILE" | head -1)"
[ -n "$BACKUP_DIR" ] || die "BACKUP_DIR not set in $ENV_FILE"
MAX_AGE="$(sed -n 's/^BACKUP_MAX_AGE_HOURS=//p' "$ENV_FILE" | head -1)"; MAX_AGE="${MAX_AGE:-26}"
command -v python3 >/dev/null 2>&1 || die "python3 not installed on this host"
exec python3 "$HERE/backup_retention.py" status \
  --dest "$BACKUP_DIR/$PROFILE" --profile "$PROFILE" --max-age-hours "$MAX_AGE"
