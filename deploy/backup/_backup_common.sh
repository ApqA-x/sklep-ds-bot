#!/usr/bin/env bash
# Помощники T14 backup/restore. Только явные пути/проекты (как _common.sh):
# из случайного cwd ничего не запускается. Секреты не печатаются никогда:
# ключ age — файлом 600 (AGE-SECRET-KEY-…), внутренние URI живут в env контейнеров.

BACKUP_HELPERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

dump_mongo_archive() { # mongo-контейнер живёт во время заморозки — exec ok
  compose exec -T mongo mongodump --quiet --db "$MONGO_DB" --archive > "$1"
  [ -s "$1" ] || die "mongodump produced empty archive"
}

media_archive() { # frozen-сервис exec'нуть нельзя — одноразовый контейнер с тем же volume
  compose run --rm --no-deps -T --entrypoint tar gateway -cf - -C /data/media . > "$1"
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
age_encrypt() { # age_encrypt IN OUT — симметрично по ключ-файлу (неинтерактивно)
  age --encrypt -i "$BACKUP_AGE_KEY_FILE" -o "$2" "$1"
}

age_decrypt_stream() { # stdout → расшифрованный поток (для restore)
  age --decrypt -i "$BACKUP_AGE_KEY_FILE" "$1"
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
