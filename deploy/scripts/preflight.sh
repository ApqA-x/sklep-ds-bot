#!/usr/bin/env bash
# T13 preflight: проверяет окружение ДО любых изменений. Ничего не меняет.
#   deploy/scripts/preflight.sh <production|staging>
# 1) docker/daemon; 2) env-файл (ключи/формат, без печати значений);
# 3) рендер compose + инварианты P01/P02/P07 (validate_compose.py);
# 4) существование и platform каждого pinned-образа в registry (без скачивания).
set -euo pipefail
source "$(dirname "$0")/_common.sh"
resolve_env "${1:-}"
[ -f "$ENV_FILE" ] || die "env file missing: $ENV_FILE (копируйте *.example и заполните; chmod 600)"

require_docker
info "project=$PROJECT file=$COMPOSE_FILE env=$ENV_FILE"

python3 "$DEPLOY_DIR/scripts/validate_env.py" --mode "$PROFILE" "$ENV_FILE"

RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
compose config --format json >"$RENDERED" 2>/dev/null
info "compose rendered: $(compose config --services | sort | tr '\n' ' ')"
python3 "$DEPLOY_DIR/scripts/validate_compose.py" --mode "$PROFILE" --json-file "$RENDERED"

# п.9: digest существует в registry и содержит нужную platform-вариант.
# docker info даёт «x86_64», манифесты — «amd64»: нормализуем.
OS="$(docker info --format '{{.OSType}}')"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) ARCH=amd64 ;;
  aarch64) ARCH=arm64 ;;
esac
TARGET_PLATFORM="$OS/$ARCH"
grep -Eo '^(MONGO_IMAGE|NATS_IMAGE|BOT_[A-Z]+_IMAGE|WEB_IMAGE)=.*' "$ENV_FILE" | while IFS='=' read -r var image; do
  [ -n "${image:-}" ] || continue
  info "checking image $var ($TARGET_PLATFORM)"
  docker buildx imagetools inspect "$image" >/dev/null 2>&1 \
    || die "$var: digest not pullable (private registry? войдите: docker login ghcr.io) или не существует"
  docker buildx imagetools inspect "$image" 2>/dev/null | grep -q "Platform: $TARGET_PLATFORM" \
    || die "$var: нет platform-варианта $TARGET_PLATFORM — не выкатывать на эту машину вслепую"
done

info "PREFLIGHT OK (mode=$PROFILE)"
