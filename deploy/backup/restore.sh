#!/usr/bin/env bash
# T14 backup/restore — ВОССТАНОВЛЕНИЕ В ИЗОЛИРОВАННУЮ ЦЕЛЬ (п.5/п.6).
#
# Никогда не пишет в живой destination: целевая БД обязана быть ПУСТОЙ (проверка
# getCollectionNames). Для DR на чистом хосте цель тоже новая — пустая БД, поэтому
# проверка одна, без «разрешающих» обходов.
#
# Целостность проверяется ДО записи чего-либо: manifest check + sha256 расшифровки
# (B04: битый архив виден до того, как что-то восстановлено).
#
# usage: restore.sh <production|staging> [--from RUN_DIR] [--into-db NAME]
#        [--media-volume NAME] [--keep] [--no-verify]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/_common.sh
source "$HERE/../scripts/_common.sh"
source "$HERE/_backup_common.sh"

resolve_env "${1:-}"; shift || true
RUN_DIR="" INTO_DB="" MEDIA_VOL="" KEEP=0 DO_VERIFY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --from) RUN_DIR="$2"; shift 2 ;;
    --into-db) INTO_DB="$2"; shift 2 ;;
    --media-volume) MEDIA_VOL="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --no-verify) DO_VERIFY=0; shift ;;
    *) die "restore.sh: неизвестный аргумент $1" ;;
  esac
done

require_docker
load_backup_env

DEST="$BACKUP_DIR/$PROFILE"
if [ -z "$RUN_DIR" ]; then
  RUN_DIR="$(host_python - "$DEST" "$PROFILE" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path
best, best_ts = None, None
for d in sorted(Path(sys.argv[1]).glob(f"dsbot-{sys.argv[2]}-*")):
    if not (d / ".verified_ok").exists():
        continue
    try:
        ts = datetime.fromisoformat(
            json.loads((d / "manifest.json").read_text())["createdAtUtc"].replace("Z", "+00:00"))
    except Exception:
        continue
    if best_ts is None or ts > best_ts:
        best, best_ts = d, ts
print(best if best else "")
PY
)"
  [ -n "$RUN_DIR" ] || die "нет ни одной проверенной точки в $DEST"
fi
[ -d "$RUN_DIR" ] || die "run dir missing: $RUN_DIR"
[ -f "$RUN_DIR/.verified_ok" ] || die "каталог не помечен .verified_ok — не точка восстановления"

info "точка: $RUN_DIR"
# --- ДО записи: манифест и checksum'ы (B04) ---
host_python "$HERE/backup_manifest.py" check --run-dir "$RUN_DIR"
host_python - "$RUN_DIR/manifest.json" > "$RUN_DIR.restore.files.json" <<'PY'
import json, sys
print(json.dumps(json.load(open(sys.argv[1]))["files"]))
PY
trap 'rm -f "$RUN_DIR.restore.files.json"' EXIT
verify_checksums "$RUN_DIR" "$RUN_DIR.restore.files.json"
info "checksums верифицированы до начала записи"

SRC_DB="$(host_python -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["db"])' "$RUN_DIR/manifest.json")"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
[ -n "$INTO_DB" ] || INTO_DB="${SRC_DB}_t14rehearsal_${TS:2:8}"
[ "$INTO_DB" != "$SRC_DB" ] || die "target db совпадает с источником — живой destination запрещён (п.5)"

# целевая БД обязана быть пуста
NAMES="$(compose exec -T mongo mongosh --quiet --eval "printjson(db.getSiblingDB('$INTO_DB').getCollectionNames().length)")"
[ "$NAMES" = "0" ] || die "целевая БД $INTO_DB не пуста ($NAMES коллекций) — перезапись запрещена"

t0=$SECONDS
info "restore mongodump-архива: $SRC_DB → $INTO_DB"
age_decrypt_stream "$RUN_DIR/mongo.archive.age" \
  | compose exec -T mongo mongorestore --quiet --drop --archive \
      --nsInclude "${SRC_DB}.*" --nsFrom "${SRC_DB}.*" --nsTo "${INTO_DB}.*"

MEDIA_STATE="$(host_python -c 'import json,sys; print(json.load(open(sys.argv[1]))["media"]["files"])' "$RUN_DIR/manifest.json")"
[ -n "$MEDIA_VOL" ] || MEDIA_VOL="dsbot-$PROFILE-restore-media-$TS"
if [ "$MEDIA_STATE" != "0" ]; then
  docker volume create "$MEDIA_VOL" >/dev/null
  info "restore media → volume $MEDIA_VOL ($MEDIA_STATE файлов)"
  age_decrypt_stream "$RUN_DIR/media.age" \
    | docker run --rm -i -v "$MEDIA_VOL:/dst" "$MONGO_IMAGE" tar -xf - -C /dst
else
  info "media в снимке нет — volume не восстанавливаем"
  MEDIA_VOL=""
fi
RESTORE_SECS=$((SECONDS - t0))
info "restore занял ${RESTORE_SECS}s (B07 evidence)"

if [ "$DO_VERIFY" = 1 ]; then
  info "verify восстановленной копии против манифеста (п.6)"
  MOUNT_ARGS=()
  RESTORE_MEDIA=""
  if [ -n "$MEDIA_VOL" ]; then
    MOUNT_ARGS=(-v "$MEDIA_VOL:/restore/media:ro")
    RESTORE_MEDIA=/restore/media
  fi
  # ${RESTORE_MEDIA:+…} раскрывает КОНТЕЙНЕРНЫЙ sh (строка в одинарных кавычках
  # хоста), поэтому пустое значение не оставляет висячий --media-dir.
  compose run --rm --no-deps -T \
    -e RESTORE_DB="$INTO_DB" -e RESTORE_MEDIA="$RESTORE_MEDIA" "${MOUNT_ARGS[@]}" gateway sh -c \
    'cat > /tmp/manifest.json && python -m voice_tracker.backup_report verify \
       --uri "$MONGO_URI" --db "$RESTORE_DB" --manifest /tmp/manifest.json \
       ${RESTORE_MEDIA:+--media-dir "$RESTORE_MEDIA"}' \
    < "$RUN_DIR/manifest.json"
  info "verify OK"
fi

if [ "$KEEP" = 0 ]; then
  info "убираем rehearsal-цели (перезапуск с --keep оставил бы их)"
  compose exec -T mongo mongosh --quiet --eval "db.getSiblingDB('$INTO_DB').dropDatabase()" >/dev/null
  [ -n "$MEDIA_VOL" ] && docker volume rm "$MEDIA_VOL" >/dev/null
fi
info "restore+verify завершены за ${RESTORE_SECS}s"
