#!/usr/bin/env bash
# T13/P04-P05: подготовка чистого Linux-хоста к первому запуску.
# Идемпотентно. Требует docker + права на volumes. checkout исходников НЕ нужен —
# только каталог deploy/ (файлы) и env-файл.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
resolve_env "${1:-}"
require_docker
[ -f "$ENV_FILE" ] || die "env file missing: $ENV_FILE"
python3 "$DEPLOY_DIR/scripts/validate_env.py" --mode "$PROFILE" "$ENV_FILE"

MEDIA_VOLUME="$(grep -Eo '^MEDIA_VOLUME=.*' "$ENV_FILE" | cut -d= -f2-)"
MONGO_VOLUME="$(grep -Eo '^MONGO_VOLUME=.*' "$ENV_FILE" | cut -d= -f2-)"
UID_WANT="$(grep -Eo '^DSBOT_UID=.*' "$ENV_FILE" | cut -d= -f2-)"
GID_WANT="$(grep -Eo '^DSBOT_GID=.*' "$ENV_FILE" | cut -d= -f2-)"
[ -n "$MEDIA_VOLUME" ] && [ -n "$MONGO_VOLUME" ] || die "MEDIA_VOLUME/MONGO_VOLUME must be set in $ENV_FILE"

info "volumes: media=$MEDIA_VOLUME mongo=$MONGO_VOLUME (uid=$UID_WANT gid=$GID_WANT)"
docker volume create "$MEDIA_VOLUME" >/dev/null
docker volume create "$MONGO_VOLUME" >/dev/null

# P05: media принадлежит ожидаемому uid/gid — проверка ДЕЙСТВИЕМ (запись/чтение
# от имени этого uid/gid), а не «volume с именем существует».
docker run --rm -u "$UID_WANT:$GID_WANT" -v "$MEDIA_VOLUME:/data" alpine \
  sh -c 'touch /data/.dsbot-write-check && rm /data/.dsbot-write-check' \
  || die "uid=$UID_WANT gid=$GID_WANT не может писать в $MEDIA_VOLUME: chown -R $UID_WANT:$GID_WANT \$(docker volume inspect -f '{{.Mountpoint}}' $MEDIA_VOLUME)"
info "media volume: запись от имени $UID_WANT:$GID_WANT подтверждена"

# web читает тот же volume ro (проверяется на up в runbook'е, P05-B)
if [ "$PROFILE" = "production" ]; then
  info "не забыть (runbook): docker/systemd включён автозапуском (systemctl enable docker),"
  info "иначе после reboot контейнеры не поднимутся сами; секреты — только $ENV_FILE (600)."
fi
info "linux_init OK — дальше: deploy/scripts/deploy.sh $PROFILE --apply"
