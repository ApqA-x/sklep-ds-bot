#!/usr/bin/env bash
# T14 backup/restore — СОЗДАНИЕ ТОЧКИ ВОССТАНОВЛЕНИЯ.
#
# Граница согласованности (п.3): writers (tracker/writer/commands/gateway/
# activity/stalker/web) останавливаются до снимка Mongo и медиа, Mongo остаётся
# запущенной (даёт mongodump). Снимок берётся при нулевой записи, поэтому
# «consistent snapshot: true» честно только для этого окна. После финализации
# writers поднимаются обратно.
#
# Атомарность (п.4): архив пишется в каталог <...>.incomplete; manifest.json
# составляется и проверяется; только затем mv в финальное имя и создаётся
# sidecar .verified_ok. Сбой на любом шаге не трогает предыдущую проверенную
# копию (B02) и не оставляет полуточку с sidecar; упавший каталог остаётся
# виден оператору как orphan.
#
# R26-09 (приватность и параллелизм):
#  * stream-encryption: stdout mongodump и tar сразу уходит в age — на диск
#    попадают только mongo.archive.age / media.age, plaintext-стейджинга нет
#    ни при каком исходе (включая SIGKILL: trap не нужен, чтобы нечего стирать);
#  * preflight ДО freeze writers — полный (шаг 2 AI_RELEASE_REMAINING_WORK,
#    добор PR #74): ключ (round-trip encrypt→decrypt), инструменты в их реальном
#    контексте исполнения (mongodump в контейнере mongo, tar в образе gateway),
#    запись в BACKUP_DIR и свободное место не менее BACKUP_MIN_FREE_MB
#    (default 64 MiB). Невалидный ключ, отсутствующий бинарник или полный диск
#    видны, пока стенд ещё работает: отказ даёт ненулевой выход при нулевом
#    числе compose stop и без .incomplete-каталога;
#  * ops-lock (flock -x -n на $BACKUP_DIR/.ops-<profile>.lock) держится весь
#    прогон: параллельный backup/restore/retention-вне-прогона не запускаются;
#    без flock скрипт отказывается стартовать (fail-closed).
#
# Нигде не печатаются секреты: age-ключ читается с диска, URI — из env
# контейнеров (наружу не выводятся).
#
# usage: backup.sh <production|staging>
set -euo pipefail
# права файлов точки (700/600) — с первой команды, до любого создания файлов
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/_common.sh
source "$HERE/../scripts/_common.sh"
source "$HERE/_backup_common.sh"

resolve_env "${1:-}"; shift || true
[ $# -eq 0 ] || { echo "backup.sh: лишних аргументов нет — только профиль" >&2; exit 2; }

require_docker
load_backup_env
# R26-09: единый lock на профиль против параллельных прогонов (держим до выхода)
acquire_ops_lock
# R26-09: кривой age-ключ обнаруживается ДО остановки writers (стенд не лежит впустую)
age_key_preflight
# R26-09 (добор PR #74): инструменты (mongodump/tar в реальных контекстах) и
# свободное место в приёмнике — тоже ДО остановки writers
preflight_tools_and_space

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_DIR/$PROFILE"
mkdir -m 700 -p "$DEST"
FINAL="$DEST/dsbot-$PROFILE-$TS"
# .incomplete (не скрытая точка): упавший запуск остаётся manifest-less или
# unverified-записью и виден оператору через backup_status (B06-форензика);
# retention такие каталоги не удаляет.
PARTIAL="$DEST/dsbot-$PROFILE-$TS.incomplete"
WORK="$(mktemp -d)"

APPS="$(services_to_freeze)"
UNFROZEN=0
RUNNING=""
freeze() {
  # поднимаем после дампа ровно то, что работало до заморозки (оператор мог
  # намеренно держать сервис остановленным)
  RUNNING="$(compose ps --services | tr '\n' ' ')"
  info "freeze writers: $APPS"
  compose stop $APPS >/dev/null
}
unfreeze() {
  [ "$UNFROZEN" = 1 ] && return 0
  UNFROZEN=1
  [ -n "$RUNNING" ] || return 0
  info "unfreeze: поднимаем ранее работавшие: $RUNNING"
  compose up -d $RUNNING >/dev/null 2>&1 || die "не удалось поднять ($RUNNING) — требовать вмешательства"
}
# При любом выходе (включая ошибку в середине) writers поднимаем и work-каталог
# убираем — иначе стенд останется замороженным. scrub_partial_plaintext —
# страховка R26-09: в стрим-конвейере plaintext не возникает, но если артефакт
# прежнего конвейера всё же есть в полуточке, он не должен пережить прогон.
trap 'unfreeze; rm -rf "$WORK"; scrub_partial_plaintext "$PARTIAL" || true' EXIT

mkdir -m 700 "$PARTIAL"
t0=$SECONDS

freeze
info "writers остановлены; снимаем Mongo и media (stream-encrypt: plaintext на диск не пишется)"

# 1) Mongo — архив в stdout mongodump (внутри mongo-контейнера mongodump есть),
#    сразу в age: на диске только шифротекст mongo.archive.age; падение любой
#    половины конвейера = ненулевой выход (pipefail).
dump_mongo_archive "$PARTIAL/mongo.archive.age"
info "mongodump|age: $(wc -c < "$PARTIAL/mongo.archive.age") bytes ciphertext"

# 2) Media — читается volume одноразовым контейнером с образом приложения
#    (единственный пишущий сервис уже заморожен); tar-поток идёт напрямую в
#    age → media.age (media.tar на диске не появляется).
media_archive "$PARTIAL/media.age"
info "media|age: $(wc -c < "$PARTIAL/media.age") bytes ciphertext"

# 3) counts/схема — из замороженного состояния, одноразовым контейнером с
#    voice_tracker (pymongo есть в образе), к БД по внутреннему URI контейнера.
counts_json "$WORK/counts.json"
media_manifest_json "$WORK/media.json"
app_revision="$(container_label gateway org.opencontainers.image.revision || true)"
unfreeze
DUMP_SECS=$((SECONDS - t0))
info "writers подняты (окно заморозки ${DUMP_SECS}s)"

# 4) Sha256 — по шифротексту: он единственный виден диску с шагов 1–2 (R26-09),
#    отдельные plaintext-копии стирать нечем.
sha256sum "$PARTIAL/mongo.archive.age" | awk '{print $1}' > "$WORK/sha_mongo"
sha256sum "$PARTIAL/media.age" | awk '{print $1}' > "$WORK/sha_media"
cat > "$WORK/files.json" <<EOF
[
  {"name":"mongo.archive.age","sha256Encrypted":"$(cat "$WORK/sha_mongo")","bytes":$(wc -c < "$PARTIAL/mongo.archive.age")},
  {"name":"media.age","sha256Encrypted":"$(cat "$WORK/sha_media")","bytes":$(wc -c < "$PARTIAL/media.age")}
]
EOF

cat > "$WORK/consistency.json" <<EOF
{"writersFrozen": true, "frozenServices": [$(printf '"%s",' $APPS | sed 's/,$//')], "freezeWindowSeconds": $DUMP_SECS, "consistentSnapshot": true}
EOF
tools_json "$WORK/tools.json"
cat > "$WORK/durations.json" <<EOF
{"freezeWindowSeconds": $DUMP_SECS}
EOF

# 5) manifest (п.2). build отказывается писать при признаках секретов (B01).
manifest_build "$PARTIAL/manifest.json" "$TS" "$WORK" "$app_revision"

# 6) П.5: перечитать зашифрованный архив (checksum) ДО объявления успешным.
#    Расхождение → B02: предыдущая копия цела, эта не финализируется.
verify_checksums "$PARTIAL" "$WORK/files.json"
host_python "$HERE/backup_manifest.py" check --run-dir "$PARTIAL" >/dev/null

# 7) Финализация: mv во временное→финальное имя, затем sidecar.
mv "$PARTIAL" "$FINAL"
touch "$FINAL/.verified_ok"
info "backup written: $FINAL"

# 8) Ретенция (п.8): GFS; последняя проверенная защищена от удаления.
#    Вызывается из этого же shell — ops-lock (fd 9) уже удерживается прогоном,
#    повторного захвата нет (R26-09).
host_python "$HERE/backup_retention.py" prune --dest "$DEST" --profile "$PROFILE" \
  --daily-keep "$RETENTION_DAILY" --weekly-keep "$RETENTION_WEEKLY" --execute

info "backup OK: $FINAL (${DUMP_SECS}s freeze window)"
