#!/usr/bin/env bash
# T14/R26-08 — ВОССТАНОВЛЕНИЕ ТОЛЬКО В ЦЕЛИ, СОЗДАННЫЕ ЭТИМ ПРОГОНОМ.
#
# Модель рисков (R26-08): `docker volume create` молча возвращает СУЩЕСТВУЮЩИЙ
# volume, а cleanup по имени из аргумента мог удалить чужой ресурс. Поэтому:
#  * media-volume всегда НОВЫЙ dsbot-<profile>-restore-media-<runid>; имя извне
#    не принимается (--media-volume удалён);
#  * цель БД — voice_tracker_<profile>_rehearsal_<runid> (или --into-db,
#    прогнанный через строгий allowlist restore_targets.py validate-db);
#  * перед любой записью: манифест+checksums, validate-db, listDatabases
#    (цель обязана отсутствовать), volume inspect (обязан ОТКАЗАТЬ), и только
#    потом state-write prepared (резервация прогона) и сами записи;
#  * после volume create метка com.dsbot.restore.run перечитывается — создан
#    не нами = отказ (защита от «create вернул существующий» и от гонки);
#  * гейт listDatabases повторяется непосредственно перед mongorestore
#    (double-check гонки); mongorestore БЕЗ --drop — цель гарантированно пуста;
#  * cleanup удаляет ТОЛЬКО цели, которые state-файл прогона подтверждает как
#    созданные им (run id + фаза db-restored + ownership-метка volume);
#  * verify обязателен всегда: флага обхода нет (--no-verify удалён),
#    отказ verify = ненулевой exit, диагностика в state-файле не затирается;
#  * cutover (перенос на чистый хост) принимает целевое prod-имя только с
#    явным --confirm-dest <точное имя>; cleanup целей в cutover запрещён
#    (--keep форсится), существующие ресурсы не удаляются никогда.
#
# usage: restore.sh <production|staging> [--from RUN_DIR]
#        [--mode rehearsal|cutover] [--into-db NAME] [--state FILE] [--resume]
#        [--keep] [--confirm-dest NAME]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/_common.sh
source "$HERE/../scripts/_common.sh"
source "$HERE/_backup_common.sh"

TARGETS="$HERE/restore_targets.py"

# Канон проекта для интерполяции имени БД в mongosh --eval (dropDatabase):
# regex допускает ТОЛЬКО [A-Za-z0-9_], поэтому кавычка/;. в JS-строку попасть
# физически не могут — после проверки имени этим regex интерполяция безопасна.
DB_ALLOWLIST_RE='^voice_tracker_(production|staging)_rehearsal_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{6}$'
DB_SAFE_RE='^[A-Za-z0-9_]+$'
RUNID_RE='^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{6}$'
db_name_is_safe() { [[ "$1" =~ $DB_ALLOWLIST_RE ]]; }

usage_restore() {
  cat >&2 <<'EOF'
usage: restore.sh <production|staging> [--from RUN_DIR]
        [--mode rehearsal|cutover (default rehearsal)] [--into-db NAME]
        [--state FILE] [--resume] [--keep] [--confirm-dest NAME]
  verify обязателен всегда; --no-verify и --media-volume удалены (R26-08).
EOF
}

resolve_env "${1:-}" || { usage_restore; exit 2; }
shift || true
MODE=rehearsal RUN_DIR="" INTO_DB="" STATE="" RESUME=0 KEEP=0 CONFIRM_DEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from)         [ $# -ge 2 ] || die "restore.sh: $1 требует значение"; RUN_DIR="$2"; shift 2 ;;
    --mode)         [ $# -ge 2 ] || die "restore.sh: $1 требует значение"; MODE="$2"; shift 2 ;;
    --into-db)      [ $# -ge 2 ] || die "restore.sh: $1 требует значение"; INTO_DB="$2"; shift 2 ;;
    --state)        [ $# -ge 2 ] || die "restore.sh: $1 требует значение"; STATE="$2"; shift 2 ;;
    --confirm-dest) [ $# -ge 2 ] || die "restore.sh: $1 требует значение"; CONFIRM_DEST="$2"; shift 2 ;;
    --resume)       RESUME=1; shift ;;
    --keep)         KEEP=1; shift ;;
    -h|--help)      usage_restore; exit 0 ;;
    *)              die "restore.sh: неизвестный аргумент $1 (verify обязателен: --no-verify удалён; media-volume всегда новый: --media-volume удалён)" ;;
  esac
done
case "$MODE" in
  rehearsal) ;;
  cutover)
    # prod-имя цели принимается ТОЛЬКО с явным точным подтверждением; cleanup
    # целей в cutover запрещён — форсируем keep и фиксируем это в отчёте.
    [ -n "$CONFIRM_DEST" ] || die "cutover требует --confirm-dest <точное целевое имя>"
    KEEP=1
    ;;
  *) die "restore.sh: --mode rehearsal|cutover (получено $MODE)" ;;
esac
[ -z "$CONFIRM_DEST" ] || [ "$MODE" = cutover ] \
  || die "--confirm-dest имеет смысл только с --mode cutover"

require_docker
load_backup_env

# стабильный по профилю default: повторный запуск без --resume упрётся в
# чужой run id в state-файле (защита «повтор в один час», R26-08 п.4)
STATE="${STATE:-$BACKUP_DIR/.restore-$PROFILE-state.json}"

WORK=""
PHASE=start
RUNID="" MEDIA_VOL="" SRC_DB="" MEDIA_FILES=0 MEDIA_BYTES=0
STATE_STARTED=0 REACHED=""
COMPLETED=0

# ---------- read-only помощники ----------
state_field() { host_python "$TARGETS" state-read --state "$STATE" --field "$1"; }

state_write() { # state_write PHASE
  host_python "$TARGETS" state-write --state "$STATE" --run-id "$RUNID" \
    --db "$INTO_DB" --volume "${MEDIA_VOL:-}" --mode "$MODE" --profile "$PROFILE" \
    --run-dir "$RUN_DIR" --phase "$1"
}

has_phase() { case " $REACHED " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# один безопасный вызов: строка --eval — фиксированный литерал, НИКАКОЙ
# интерполяции имён БД в JS (инъекция через имя цели исключена структурно).
# R26-07: mongod под --auth — listDatabases требует привилегий, гейт идёт по
# MONGO_ADMIN_URI из env-файла (значение секретно, в вывод не попадает).
list_databases() {
  [ -n "${MONGO_ADMIN_URI:-}" ] \
    || die "MONGO_ADMIN_URI not set (R26-07: mongosh gates need admin credentials)"
  compose exec -T mongo mongosh --quiet --uri "$MONGO_ADMIN_URI" --eval \
    'db.adminCommand({listDatabases:1}).databases.map(d=>d.name).join(" ")'
}

db_must_be_absent() {
  local dbs d exists
  dbs="$(list_databases)"
  exists=false
  for d in $dbs; do
    if [ "$d" = "$INTO_DB" ]; then exists=true; fi
  done
  host_python "$TARGETS" assert-absent --exists "$exists"
}

volume_run_label() { # → stdout: значение ownership-метки; rc!=0 — volume отсутствует
  local out
  out="$(docker volume inspect -f '{{ index .Labels "com.dsbot.restore.run" }}' "$1")" || return 1
  if [ "$out" = "<no value>" ]; then out=""; fi
  printf '%s' "$out"
}

# ---------- cleanup: только цели, подтверждённые state этого прогона ----------
cleanup_owned_targets() {
  local all_ok=1 lbl
  # БД: имя повторно проходит allowlist-regex (критерий безопасности --eval) +
  # state подтверждает, что ЭТОТ прогон дошёл до db-restored для ЭТОЙ цели.
  if [ -n "$INTO_DB" ] && db_name_is_safe "$INTO_DB" && [ "$STATE_STARTED" = 1 ] \
     && host_python "$TARGETS" state-check --state "$STATE" --run-id "$RUNID" \
          --db "$INTO_DB" --need-phase db-restored >/dev/null 2>&1; then
    # R26-07: drop под --auth — только admin-сессией; в cleanup-ветке НЕ die
    # (иначе затёрла бы исходную причину отказа в finish/trap): пустой URI или
    # сбой mongosh → warn + all_ok=0, цель остаётся под наблюдением.
    if [ -n "${MONGO_ADMIN_URI:-}" ] && compose exec -T mongo mongosh --quiet \
         --uri "$MONGO_ADMIN_URI" --eval \
         "db.getSiblingDB('$INTO_DB').dropDatabase()" >/dev/null; then
      info "cleanup: rehearsal-БД $INTO_DB удалена"
    else
      warn "cleanup: drop $INTO_DB не удался — цель осталась, разбираемся вручную"
      all_ok=0
    fi
  else
    info "cleanup: БД $INTO_DB НЕ удаляем (не подтверждена целью этого прогона)"
  fi
  if [ -n "$MEDIA_VOL" ]; then
    if lbl="$(volume_run_label "$MEDIA_VOL")"; then
      # никогда не rm ресурса, который state не подтверждает как созданный им:
      # метка run == RUNID прогона + тот же volume записан в state
      if [ "$lbl" = "$RUNID" ] \
         && host_python "$TARGETS" state-check --state "$STATE" --run-id "$RUNID" \
              --volume "$MEDIA_VOL" >/dev/null 2>&1; then
        if docker volume rm "$MEDIA_VOL" >/dev/null; then
          info "cleanup: media-volume $MEDIA_VOL удалён"
        elif volume_run_label "$MEDIA_VOL" >/dev/null 2>&1; then
          # rm вернул ошибку при живом volume: цель НЕ удалена — cleaned не
          # пишем, иначе resume ложно заблокируется и осиротевшая цель выпадет
          # из наблюдения (по образцу drop-ветки выше)
          warn "cleanup: volume $MEDIA_VOL не удалён — цель осталась, разбираемся вручную"
          all_ok=0
        else
          info "cleanup: volume $MEDIA_VOL уже отсутствует — удалять нечего"
        fi
      else
        warn "cleanup: volume $MEDIA_VOL — ownership не подтверждён; НЕ трогаем"
        all_ok=0
      fi
    else
      info "cleanup: volume $MEDIA_VOL отсутствует — удалять нечего"
    fi
  fi
  if [ "$all_ok" = 1 ] && [ "$STATE_STARTED" = 1 ]; then
    state_write cleaned >/dev/null 2>&1 || warn "cleanup: state cleaned не записан"
  fi
}

# ---------- отказ: failed-state + cleanup только своих целей ----------
finish() {
  local rc=$?
  if [ -n "$WORK" ]; then
    rm -rf "$WORK"  # plaintext media на диске не оставляем ни при каком исходе
  fi
  if [ "$rc" = 0 ] || [ "$COMPLETED" = 1 ]; then
    return 0
  fi
  if [ "$STATE_STARTED" = 1 ]; then
    host_python "$TARGETS" state-write --state "$STATE" --run-id "$RUNID" \
      --db "${INTO_DB:-}" --volume "${MEDIA_VOL:-}" --mode "$MODE" --profile "$PROFILE" \
      --run-dir "$RUN_DIR" --phase "failed:$PHASE" >/dev/null 2>&1 \
      || warn "failed-state не удалось записать в $STATE"
  fi
  if [ "$MODE" = rehearsal ] && [ "$KEEP" = 0 ]; then
    cleanup_owned_targets || true
  else
    warn "цели НЕ удаляем (mode=$MODE keep=$KEEP): ${INTO_DB:-?} / ${MEDIA_VOL:-без media}"
  fi
  info "restore FAILED: exit=$rc фаза=$PHASE state=$STATE (диагностика сохранена)"
  return "$rc"
}
trap finish EXIT

# ---------- шаг 1: точка, манифест, checksums — ДО любых записей ----------
WORK="$(mktemp -d)"
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

PHASE=manifest
info "точка: $RUN_DIR"
host_python "$HERE/backup_manifest.py" check --run-dir "$RUN_DIR"
host_python - "$RUN_DIR/manifest.json" > "$WORK/files.json" <<'PY'
import json, sys
print(json.dumps(json.load(open(sys.argv[1]))["files"]))
PY
verify_checksums "$RUN_DIR" "$WORK/files.json"
info "checksums верифицированы до начала записи"
read -r SRC_DB MEDIA_FILES MEDIA_BYTES <<EOF
$(host_python -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m["source"]["db"], int(m["media"]["files"]), int(m["media"]["bytes"]))' "$RUN_DIR/manifest.json")
EOF
[ -n "$SRC_DB" ] && [ -n "$MEDIA_FILES" ] && [ -n "$MEDIA_BYTES" ] \
  || die "manifest: не читаются source.db / media.files / media.bytes"

# ---------- шаг 2: цели прогона (resume или новый run id) ----------
PHASE=targets
if [ "$RESUME" = 1 ]; then
  [ -f "$STATE" ] || die "resume: state-файл не найден: $STATE"
  RUNID="$(state_field runId)"
  INTO_DB="$(state_field db)"
  MEDIA_VOL="$(state_field volume)"
  REACHED="$(state_field reached)"
  [ "$(state_field cleaned)" != "true" ] \
    || die "resume: прогон уже завершён cleanup'ом (cleaned) — сделайте новый запуск с новым --state"
  [ "$RUNID" != "" ] && [ "$INTO_DB" != "" ] || die "resume: state не содержит полных целей"
  [ "$(state_field mode)" = "$MODE" ] || die "resume: режим прогона в state ($MODE запросили) не совпадает"
  s_run_dir="$(state_field runDir)"
  [ -z "$s_run_dir" ] || [ "$s_run_dir" = "$RUN_DIR" ] \
    || die "resume: state привязан к другой точке ($s_run_dir)"
  [[ "$RUNID" =~ $RUNID_RE ]] || die "resume: run id в state повреждён"
  if [ "$MODE" = rehearsal ]; then
    [ "$INTO_DB" = "voice_tracker_${PROFILE}_rehearsal_${RUNID}" ] \
      || die "resume: цель БД в state не совпадает с восстановленной по run id"
    [ -z "$MEDIA_VOL" ] || [ "$MEDIA_VOL" = "dsbot-$PROFILE-restore-media-$RUNID" ] \
      || die "resume: media-volume в state не совпадает с восстановленным по run id"
  else
    [ "$INTO_DB" = "$CONFIRM_DEST" ] || die "resume: cutover-цель не подтверждена --confirm-dest"
  fi
  if [ "$MEDIA_FILES" != "0" ] && [ -z "$MEDIA_VOL" ]; then
    die "resume: точка содержит media, но media-цель в state не записана"
  fi
  STATE_STARTED=1
  info "resume: run=$RUNID db=$INTO_DB media=${MEDIA_VOL:-нет} reached=$REACHED"
else
  RUNID="$(host_python "$TARGETS" run-id)"
  [ -n "$INTO_DB" ] || INTO_DB="voice_tracker_${PROFILE}_rehearsal_${RUNID}"
  if [ "$MEDIA_FILES" != "0" ]; then
    MEDIA_VOL="dsbot-$PROFILE-restore-media-$RUNID"
    [ "$MEDIA_VOL" != "$(env_value MEDIA_VOLUME)" ] \
      || die "media-volume совпал с рабочим из env — невозможно (ошибка сборки имени)"
  fi
fi
if [ "$MEDIA_FILES" = "0" ]; then
  MEDIA_VOL=""
  info "media в снимке нет — volume не создаём"
fi

# ---------- валидация имён целей ----------
PHASE=validate
if [ "$MODE" = rehearsal ]; then
  host_python "$TARGETS" validate-db --db "$INTO_DB" --source "$SRC_DB" --configured "$MONGO_DB"
else
  [ "$INTO_DB" = "$CONFIRM_DEST" ] \
    || die "cutover: --into-db ($INTO_DB) обязано точно совпадать с --confirm-dest ($CONFIRM_DEST)"
  [[ "$INTO_DB" =~ $DB_SAFE_RE ]] || die "cutover: целевое имя содержит запрещённые символы"
fi

# ---------- гейт БД (первый) + гейт volume (отказ, если существует) ----------
if ! has_phase db-restored; then
  PHASE=db-gate
  db_must_be_absent
fi
if [ -n "$MEDIA_VOL" ] && ! has_phase media-extracted; then
  PHASE=volume-gate
  if [ "$RESUME" = 0 ] && docker volume inspect "$MEDIA_VOL" >/dev/null 2>&1; then
    die "volume $MEDIA_VOL уже существует — прогон пишет только в новые цели"
  fi
fi

# ---------- резервация прогона: дальше идут только записи в наши цели ----------
PHASE=state-prepared
if ! has_phase prepared; then
  # этот вызов и есть «резервация»: повторный запуск в тот же state-файл
  # с другим run id получает отказ (защита «повтор в один час»)
  state_write prepared >/dev/null
fi
STATE_STARTED=1

# ---------- шаги 4–5: volume с ownership-меткой, tar, chown ----------
if [ -n "$MEDIA_VOL" ] && ! has_phase media-extracted; then
  PHASE=volume
  # на свежем прогоне до create — только existence-гейт (volume-gate выше,
  # голым inspect); сверка ownership-метки — после create, ровно как требует
  # R26-08: «create молча возвращает существующий» ловится перечитыванием
  # метки, а не вторым inspect до записи.
  if [ "$RESUME" = 1 ] && lbl="$(volume_run_label "$MEDIA_VOL")"; then
    [ "$lbl" = "$RUNID" ] \
      || die "resume: volume $MEDIA_VOL существует с чужой меткой (${lbl:-<none>}) — не наш прогон"
    info "resume: volume уже создан этим прогоном"
  else
    docker volume create \
      --label "com.dsbot.restore.run=$RUNID" \
      --label "com.dsbot.restore.mode=$MODE" \
      "$MEDIA_VOL" >/dev/null
    # «create молча возвращает существующий» — перечитываем метку: создан не
    # нами = отказ, дальше cleanup этого ресурса не тронет никогда
    lbl="$(volume_run_label "$MEDIA_VOL")" \
      || die "volume $MEDIA_VOL не виден inspect после create"
    [ "$lbl" = "$RUNID" ] \
      || die "метка volume после create (${lbl:-<none>}) ≠ run id — ресурс создан НЕ этим прогоном"
  fi
  mountpoint_dir="$(docker volume inspect -f '{{.Mountpoint}}' "$MEDIA_VOL")"
  [ -n "$mountpoint_dir" ] && [ "$mountpoint_dir" != "<no value>" ] \
    || die "volume $MEDIA_VOL без Mountpoint"

  PHASE=media
  info "restore media → volume $MEDIA_VOL ($MEDIA_FILES файлов)"
  age_decrypt_stream "$RUN_DIR/media.age" > "$WORK/media.tar"
  # потолок: media.bytes из манифеста × 2 (явный параметр, проверяется до
  # первой записи в volume)
  host_python "$TARGETS" check-tar --archive "$WORK/media.tar" --max-bytes $(( MEDIA_BYTES * 2 ))
  docker run --rm -i -v "$MEDIA_VOL:/dst" "$MONGO_IMAGE" tar -xf - -C /dst < "$WORK/media.tar"
  uidv="$(env_value DSBOT_UID)"
  gidv="$(env_value DSBOT_GID)"
  { [ -n "$uidv" ] && [ -n "$gidv" ]; } \
    || die "DSBOT_UID/DSBOT_GID не заданы в $ENV_FILE — некому вернуть права gateway на media"
  [[ "$uidv" =~ ^[0-9]+$ ]] && [[ "$gidv" =~ ^[0-9]+$ ]] \
    || die "DSBOT_UID/DSBOT_GID должны быть числовыми uid/gid"
  # extraction идёт root'овым контейнером — без смены владельца gateway не сможет писать
  docker run --rm -v "$MEDIA_VOL:/dst" "$MONGO_IMAGE" ch -R "$uidv:$gidv" /dst
  rm -f "$WORK/media.tar"
  state_write media-extracted >/dev/null
fi

# ---------- шаг 6: mongorestore без --drop (цель гарантированно пуста) ----------
t0=$SECONDS
if ! has_phase db-restored; then
  PHASE=db-gate-rerace
  db_must_be_absent   # double-check гонки непосредственно перед записью в БД
  PHASE=mongorestore
  # R26-07: mongod под --auth — восстановлению нужен URI с ролями restore+
  # readAnyDatabase (dsbot_restore из env-файла); значение не печатается.
  [ -n "${MONGO_RESTORE_URI:-}" ] \
    || die "MONGO_RESTORE_URI not set (R26-07: mongorestore needs restore role)"
  info "restore mongodump-архива: $SRC_DB → $INTO_DB (цель гарантированно пуста — обход без drop)"
  age_decrypt_stream "$RUN_DIR/mongo.archive.age" \
    | compose exec -T mongo mongorestore --quiet --uri "$MONGO_RESTORE_URI" --archive \
        --nsInclude "${SRC_DB}.*" --nsFrom "${SRC_DB}.*" --nsTo "${INTO_DB}.*"
  state_write db-restored >/dev/null
else
  info "resume: mongorestore уже выполнен прогоном $RUNID"
fi

# ---------- шаг 7: verify ОБЯЗАТЕЛЕН (обхода нет) ----------
if ! has_phase verified; then
  PHASE=verify
  info "verify восстановленной копии против манифеста (обязательный)"
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
  state_write verified >/dev/null
  info "verify OK"
else
  info "resume: verify уже выполнен прогоном $RUNID"
fi
RESTORE_SECS=$((SECONDS - t0))

# ---------- отчёт; cleanup только rehearsal без --keep ----------
COMPLETED=1
if [ "$KEEP" = 0 ]; then
  info "убираем rehearsal-цели (для хранения целей — --keep)"
  cleanup_owned_targets
fi
info "──────── отчёт restore ────────"
info "run id: $RUNID | mode: $MODE | keep: $KEEP"
info "цели:   db=$INTO_DB | media=${MEDIA_VOL:-не создавался}"
info "точка:  $RUN_DIR (source db=$SRC_DB)"
info "verify: выполнен и пройден (обязателен; обхода нет)"
info "state:  $STATE"
info "restore занял ${RESTORE_SECS}s (B07 evidence)"
if [ "$MODE" = cutover ]; then
  info "cutover: цели оставлены намеренно; активация (MONGO_DB/MEDIA_VOLUME в env профиля) — отдельный шаг оператора"
fi
