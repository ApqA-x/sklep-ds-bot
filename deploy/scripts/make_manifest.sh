#!/usr/bin/env bash
# T13/п.8: собрать deployment manifest — что ИМЕННО задеплоено.
#   make_manifest.sh <production|staging> [--out deploy/manifest]
# Значения секретов не читаются и не пишутся: только *_IMAGE-переменные,
# git SHA из OCI-метки образа, schema/event версии из кода внутри образа, дата,
# целевая платформа и ссылка на предыдущий манифест.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
resolve_env "${1:-}"
shift || true
OUT="$DEPLOY_DIR/manifest"
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    *) die "неизвестный аргумент: $1" ;;
  esac
done
require_docker
[ -f "$ENV_FILE" ] || die "env file missing: $ENV_FILE"

TARGET_OS="linux"
ARCH="$(uname -m)"; case "$ARCH" in x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; esac
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"

revision_of() {
  local image="$1"
  local sha
  sha="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image" 2>/dev/null || true)"
  [ -n "$sha" ] && [ "$sha" != "<no value>" ] || { echo "unknown"; return; }
  echo "$sha"
}

entry() { # name, image -> json строка
  local git="upstream"
  case "$2" in ghcr.io/*) git="$(revision_of "$2")" ;; esac
  printf '    {"service": "%s", "image": "%s", "gitSha": "%s"}' "$1" "$2" "$git"
}

SCHEMA_LINES="\"schemaVersion\": null, \"schemaManifestChecksum\": null, \"eventSchemaVersion\": null"
GATEWAY_IMAGE="$(grep -Eo '^BOT_GATEWAY_IMAGE=.*' "$ENV_FILE" | cut -d= -f2-)"
if [ -n "$GATEWAY_IMAGE" ]; then
  docker pull --platform "$TARGET_OS/$ARCH" -q "$GATEWAY_IMAGE" >/dev/null
  info "resolving schema versions from gateway image (labels: $(revision_of "$GATEWAY_IMAGE"))"
  SV="$(docker run --rm --entrypoint python "$GATEWAY_IMAGE" -c 'import voice_tracker.schema as s; print(s.SCHEMA_VERSION)' 2>/dev/null || echo null)"
  CK="$(docker run --rm --entrypoint python "$GATEWAY_IMAGE" -c 'import voice_tracker.schema as s; print(s.manifest_checksum())' 2>/dev/null || echo null)"
  EV="$(docker run --rm --entrypoint python "$GATEWAY_IMAGE" -c 'import voice_tracker.domain as d; print(getattr(d, "EVENT_SCHEMA_VERSION", 1))' 2>/dev/null || echo null)"
  SCHEMA_LINES="\"schemaVersion\": $SV, \"schemaManifestChecksum\": \"$CK\", \"eventSchemaVersion\": $EV"
fi

{
  echo "{"
  echo "  \"createdAt\": \"$STAMP\","
  echo "  \"environment\": \"$PROFILE\","
  echo "  \"targetPlatform\": \"$TARGET_OS/$ARCH\","
  [ -f "$OUT/current.json" ] && echo "  \"previousManifest\": \"$(basename "$(readlink -f "$OUT/current.json" 2>/dev/null || echo "$OUT/current.json")")\"," || echo "  \"previousManifest\": null,"
  echo "  $SCHEMA_LINES,"
  echo "  \"images\": {"
  FIRST=1
  while IFS='=' read -r var image; do
    [ -n "${image:-}" ] || continue
    docker pull --platform "$TARGET_OS/$ARCH" -q "$image" >/dev/null
    [ $FIRST -eq 0 ] && echo ","
    FIRST=0
    entry "${var%_IMAGE}" "$image"
  done < <(grep -Eo '^(MONGO_IMAGE|NATS_IMAGE|BOT_[A-Z]+_IMAGE|WEB_IMAGE)=.*' "$ENV_FILE" | sed 's/=/ /1')
  echo ""
  echo "  },"
  echo "  \"compose\": \"$(basename "$COMPOSE_FILE")\","
  echo "  \"project\": \"$PROJECT\""
  echo "}"
} > "$OUT/manifest-$STAMP.json"

ln -sfn "manifest-$STAMP.json" "$OUT/current.json"
info "manifest: $OUT/manifest-$STAMP.json (current -> него)"
