#!/usr/bin/env bash
# Помощники T14 backup/restore. Только явные пути/проекты (как _common.sh):
# из случайного cwd ничего не запускается. Секреты не печатаются никогда:
# ключ age — файлом 600 (AGE-SECRET-KEY-…), внутренние URI живут в env контейнеров.

BACKUP_HELPERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# необязательные шаги (cleanup-diagnostic, запись состояния при уже падающем
# прогоне) не должны затыкать скрипт молча: предупреждение видно оператору,
# но не меняет ход отказа
warn() { echo "WARN: $*" >&2; }

# --- конфигурация окружения бэкапа (из того же .env профиля, что и deploy) ---
load_backup_env() {
  [ -f "$ENV_FILE" ] || die "env file missing: $ENV_FILE"
  BACKUP_DIR="$(env_value BACKUP_DIR)"
  [ -n "$BACKUP_DIR" ] || die "BACKUP_DIR not set in $ENV_FILE (host directory for backups)"
  case "$BACKUP_DIR" in /*) ;; *) die "BACKUP_DIR must be absolute: $BACKUP_DIR" ;; esac
  BACKUP_AGE_KEY_FILE="$(env_value BACKUP_AGE_KEY_FILE)"
  [ -n "$BACKUP_AGE_KEY_FILE" ] || die "BACKUP_AGE_KEY_FILE not set in $ENV_FILE"
  [ -s "$BACKUP_AGE_KEY_FILE" ] || die "age key file missing/empty: $BACKUP_AGE_KEY_FILE"
  # age -p (интерактивный passphrase) для автоматизации непригоден: читает пароль
  # только с терминала и в cron/systemd зависает навсегда. Симметричный режим
  # «--encrypt -i <ключ>» — тот же единственный секрет, но неинтерактивный.
  # age-keygen кладёт секрет не на первую строку («# created:», «# public key:» …)
  grep -q '^AGE-SECRET-KEY-' "$BACKUP_AGE_KEY_FILE" \
    || die "$BACKUP_AGE_KEY_FILE: ожидается age-ключ (age-keygen -o …), строка AGE-SECRET-KEY-…"
  MONGO_DB="$(env_value MONGO_DB)"
  [ -n "$MONGO_DB" ] || die "MONGO_DB not set in $ENV_FILE"
  MONGO_IMAGE="$(env_value MONGO_IMAGE)"
  [ -n "$MONGO_IMAGE" ] || die "MONGO_IMAGE not set in $ENV_FILE (restore media через mongo-образ)"
  # R26-07: mongod под --auth — mongodump/mongorestore/mongosh-гейты работают
  # только по аутентифицированным URI из env-файла. Обязательность проверяется
  # в месте использования (backup — MONGO_BACKUP_URI, restore — ADMIN/RESTORE),
  # значения не печатаются никогда.
  MONGO_BACKUP_URI="$(env_value MONGO_BACKUP_URI)"
  MONGO_ADMIN_URI="$(env_value MONGO_ADMIN_URI)"
  MONGO_RESTORE_URI="$(env_value MONGO_RESTORE_URI)"
  RETENTION_DAILY="$(env_value RETENTION_DAILY)"; RETENTION_DAILY="${RETENTION_DAILY:-7}"
  RETENTION_WEEKLY="$(env_value RETENTION_WEEKLY)"; RETENTION_WEEKLY="${RETENTION_WEEKLY:-4}"
  command -v age >/dev/null 2>&1 || die "age not installed on this host (encryption, п.T14 D06)"
  mkdir -p "$BACKUP_DIR"
  [ -w "$BACKUP_DIR" ] || die "BACKUP_DIR not writable: $BACKUP_DIR"
}

env_value() { # env_value KEY → значение из ENV_FILE (пусто если нет)
  sed -n "s/^${1}=//p" "$ENV_FILE" | head -1
}

host_python() {
  command -v python3 >/dev/null 2>&1 || die "python3 not installed on this host"
  python3 "$@"
}

# --- снимки ---
services_to_freeze() { # всё, что пишет (app-сервисы), кроме инфраструктуры
  compose config --services | grep -vE '^(mongo|nats)$' | tr '\n' ' ' | sed 's/ $//'
}

dump_mongo_archive() { # dump_mongo_archive AGE_OUT — R26-09: стрим в age, plaintext не касается диска
  # mongo-контейнер живёт во время заморозки — exec ok.
  # R26-07: mongod под --auth — дампу нужен URI с встроенной ролью backup
  # (dsbot_backup создан в РАБОЧЕЙ БД, authSource=<MONGO_DB> из env-примеров;
  # роль backup живёт в admin, но пользователя туда не переносит). --db остаётся:
  # в URI база не указана (путь "/"), конфликтов с --uri нет. Значение секретно и
  # в вывод не попадает (только как аргумент mongodump внутри контейнера).
  # R26-09: stdout mongodump идёт напрямую в age --encrypt — на диске появляется
  # только шифротекст. pipefail вызывающего скрипта превращает падение любой
  # половины конвейера в ненулевой выход; age пишет только шифротекст, поэтому
  # обрыв конвейера не оставляет незашифрованных данных (частичный .age — мусор).
  [ -n "${MONGO_BACKUP_URI:-}" ] \
    || die "MONGO_BACKUP_URI not set (R26-07: mongodump needs backup role)"
  compose exec -T mongo mongodump --quiet --uri "$MONGO_BACKUP_URI" --db "$MONGO_DB" --archive \
    | age --encrypt -i "$BACKUP_AGE_KEY_FILE" -o "$1"
  [ -s "$1" ] || die "mongodump produced empty archive"
}

media_archive() { # media_archive AGE_OUT — R26-09: tar-поток из контейнера сразу в age
  # frozen-сервис exec'нуть нельзя — одноразовый контейнер с тем же volume;
  # tar -c пишет в stdout, age шифрует на лету: media.tar на диске не возникает.
  compose run --rm --no-deps -T --entrypoint tar gateway -cf - -C /data/media . \
    | age --encrypt -i "$BACKUP_AGE_KEY_FILE" -o "$1"
  [ -s "$1" ] || die "media archive produced empty ciphertext"
}

counts_json() {
  compose run --rm --no-deps -T gateway sh -c \
    'python -m voice_tracker.backup_report counts --uri "$MONGO_URI" --db "$MONGO_DB"' > "$1"
}

media_manifest_json() {
  compose run --rm --no-deps -T gateway \
    python -m voice_tracker.backup_report media --dir /data/media > "$1"
}

container_label() { # container_label SERVICE KEY — OCI-метка образа сервиса (может быть пусто)
  local cid
  cid="$(compose ps -q "$1" 2>/dev/null | head -1)"
  [ -n "$cid" ] || return 1
  docker inspect -f "{{ index .Config.Labels \"$2\" }}" "$cid" 2>/dev/null | grep -v '^<nil>$'
}

# --- шифрование/целостность ---
# R26-09: пост-обработка «plaintext-файл → age» удалена намеренно: единственный
# путь шифрования — стрим (dump_mongo_archive/media_archive), незашифрованные
# данные дампа на диск не пишутся. age_decrypt_stream остаётся для restore.
age_decrypt_stream() { # stdout → расшифрованный поток (для restore)
  age --decrypt -i "$BACKUP_AGE_KEY_FILE" "$1"
}

# --- R26-09: preflight ключа и блокировка параллельных прогонов ---
age_key_preflight() {
  # Round-trip синтетики ДО freeze writers: 32 случайных байта → age --encrypt
  # → age --decrypt → побайтовое сравнение. Ловит и нечитаемый/невалидный ключ,
  # и сломанный age одним прогоном; вызывается до compose stop, поэтому отказ
  # не стоит стенду простоя. Временный каталог — mktemp -d (700 при umask 077),
  # probe-байты живут только в нём и уничтожаются здесь же.
  local d rc=1
  d="$(mktemp -d)"
  if head -c 32 /dev/urandom > "$d/probe.bin" \
     && age --encrypt -i "$BACKUP_AGE_KEY_FILE" -o "$d/probe.age" "$d/probe.bin" \
     && age --decrypt -i "$BACKUP_AGE_KEY_FILE" "$d/probe.age" > "$d/probe.out"; then
    cmp -s "$d/probe.bin" "$d/probe.out" && rc=0
  fi
  rm -rf "$d"
  [ "$rc" = 0 ] \
    || die "preflight: ключ age не читается/не валиден (round-trip encrypt→decrypt не прошёл) — backup прерван ДО остановки writers"
}

OPS_LOCK_FD=9
acquire_ops_lock() {
  # Один прогон над точками профиля в любой момент: backup.sh и restore.sh
  # берут ОДИН И ТОТ ЖЕ lock (fd наследуется дочерним retention-prune из
  # того же shell — повторного захвата нет, дедлока нет). Отказ — до freeze
  # и до любых записей. fail-closed: без flock параллельные прогоны не
  # исключить, поэтому не стартуем вовсе.
  # R26-09 (добор): имя/расположение lock-файла зеркалится в
  # backup_retention.py::ops_lock_path (dest.parent/.ops-<dest.name>.lock при
  # --dest=$BACKUP_DIR/$PROFILE) — менять строго в обоих местах сразу.
  command -v flock >/dev/null 2>&1 \
    || die "flock недоступен на хосте — backup/restore без блокировки параллельных прогонов запрещены (R26-09)"
  local lockfile="$BACKUP_DIR/.ops-$PROFILE.lock"
  exec 9>"$lockfile" || die "не удалось создать lock-файл: $lockfile"
  flock -x -n "$OPS_LOCK_FD" \
    || die "другой backup/restore уже выполняется (lock: $lockfile) — параллельные прогоны запрещены (R26-09)"
}

scrub_partial_plaintext() {
  # Страховка в trap: если в незавершённом каталоге всё же существует
  # plaintext-артефакт прежнего конвейера (mongo.archive / media.tar без
  # .age-суффикса) — стереть. Вызывается только по $PARTIAL: после mv каталога
  # с таким именем нет, готовые FINAL-архивы (…archive.age/…age) не трогаем.
  [ -n "${1:-}" ] && [ -d "$1" ] || return 0
  rm -f "$1/mongo.archive" "$1/media.tar"
}

verify_checksums() { # сверяет .age-файлы каталога с files.json ДО финализации
  local dir="$1" files="$2"
  host_python - "$dir" "$files" <<'PY'
import hashlib, json, sys
from pathlib import Path
d, f = Path(sys.argv[1]), Path(sys.argv[2])
for e in json.loads(f.read_text(encoding="utf-8")):
    p = d / e["name"]
    if not p.is_file():
        sys.exit(f"verify: missing {e['name']}")
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    if h.hexdigest() != e["sha256Encrypted"]:
        sys.exit(f"verify: sha256 mismatch for {e['name']}")
PY
}

manifest_build() { # manifest_build OUT RUNID WORKDIR APP_REVISION
  local out="$1" runid="$2" work="$3" rev="$4"
  local created schema_args=() rev_args=()
  created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local sv
  sv="$(host_python -c 'import json,sys; v=json.load(open(sys.argv[1])).get("schemaVersion"); print("" if v is None else v)' "$work/counts.json")"
  [ -n "$sv" ] && schema_args=(--schema-version "$sv")
  [ -n "$rev" ] && rev_args=(--app-revision "$rev")
  host_python "$BACKUP_HELPERS_DIR/backup_manifest.py" build \
    --out "$out" \
    --profile "$PROFILE" \
    --run-id "$runid" \
    --created-at "$created" \
    --source-db "$MONGO_DB" \
    "${schema_args[@]}" "${rev_args[@]}" \
    --counts-file "$work/counts.json" \
    --media-file "$work/media.json" \
    --consistency-file "$work/consistency.json" \
    --tools-file "$work/tools.json" \
    --durations-file "$work/durations.json" \
    --files-file "$work/files.json"
}

tools_json() { # версии инструментов в манифест (п.2)
  local mongodump_ver age_ver
  mongodump_ver="$(compose exec -T mongo mongodump --version 2>/dev/null | head -1)"
  age_ver="$(age --version 2>/dev/null)"
  host_python - "$1" "$mongodump_ver" "$age_ver" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "mongodump": sys.argv[2], "age": sys.argv[3],
    "compose": "docker compose v2", "python": sys.version.split()[0],
}) + "\n", encoding="utf-8")
PY
}
