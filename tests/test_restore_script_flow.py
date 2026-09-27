"""R26-08 flow: РЕАЛЬНЫЙ deploy/backup/restore.sh с заглушками docker/age.

Приём как в review-2026-09-26/deploy_probe.py: скрипт не копируется и не
переписывается под тест; в PATH подставляются фейковые docker/age (копируются
в WSL-нативный mktemp и получают exec-бит), все вызовы логируются, состояние
volume'ов живёт в JSON-like файлах tmp_path. Реальные тома/контейнеры не
дёргались никогда: ни один вызов не уходит за пределы фейка.

На Windows pytest гоняется python.exe → bash запускается через `wsl --exec`;
на Linux (в т.ч. внутри WSL) — напрямую. Пути конвертируются C:\\x → /mnt/c/x.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY_BACKUP = REPO / "deploy" / "backup"
sys.path.insert(0, str(DEPLOY_BACKUP))

import backup_manifest  # noqa: E402

WIN = os.name == "nt"
BRIDGE = (shutil.which("wsl") is not None) if WIN else (shutil.which("bash") is not None)

pytestmark = pytest.mark.skipif(
    not BRIDGE,
    reason="нет bash-моста (Windows: требуется WSL с python3; Linux: bash+python3)",
)

FIXED_DB = "voice_tracker_production_rehearsal_20260101T000000Z_abc123"


def psh(p: Path) -> str:
    """Путь для bash-интерпретатора: на Windows python — WSL-представление."""
    if WIN:
        s = p.as_posix()  # 'C:/Users/...'
        return f"/mnt/{s[0].lower()}/{s[2:].lstrip('/')}"
    return str(p)


FAKE_DOCKER = r"""#!/usr/bin/env bash
# Тестовая заглушка docker CLI для R26-08. Каждый вызов логируется одной
# строкой; ответы берутся из FAKE_* env и «стора» volume'ов в tmp.
printf '%s\n' "docker $*" >> "$FAKE_DOCKER_LOG"

counter() {
  local f="$FAKE_COUNTER_DIR/$1" n=0
  mkdir -p "$FAKE_COUNTER_DIR"
  if [ -f "$f" ]; then n="$(cat "$f")"; fi
  n=$((n+1)); printf '%s' "$n" > "$f"; printf '%s' "$n"
}
consume_stdin() { if [ -p /dev/stdin ]; then cat >/dev/null 2>&1 || true; fi; }

vol_labels_file() { printf '%s' "$FAKE_VOLS_DIR/$1/labels"; }
vol_exists() { [ -f "$(vol_labels_file "$1")" ]; }
vol_get() { # vol_get NAME KEY → stdout
  sed -n "s/^$2=//p" "$(vol_labels_file "$1")" | head -1
}

cmd="${1:-}"; shift || true
case "$cmd" in
  info) exit 0 ;;
  -v|--version) echo "Docker version fake"; exit 0 ;;
  volume)
    sub="${1:-}"; shift || true
    case "$sub" in
      inspect)
        fmt=""; name=""
        while [ $# -gt 0 ]; do
          case "$1" in
            -f|--format) fmt="$2"; shift 2 ;;
            *) name="$1"; shift ;;
          esac
        done
        if [ -n "${FAKE_VOL_EXISTS_ALWAYS:-}" ]; then
          case "$fmt" in
            *com.dsbot.restore.run*) echo "someone-elses-run";;
            *com.dsbot.restore.mode*) echo "someone-else";;
            *Mountpoint*) echo "/fake/mnt/$name";;
            *) printf '[{"Name":"%s"}]\n' "$name";;
          esac
          exit 0
        fi
        if vol_exists "$name"; then
          case "$fmt" in
            *com.dsbot.restore.run*) vol_get "$name" run;;
            *com.dsbot.restore.mode*) vol_get "$name" mode;;
            *Mountpoint*) echo "/fake/mnt/$name";;
            *) printf '[{"Name":"%s"}]\n' "$name";;
          esac
          exit 0
        fi
        echo "Error: No such volume: $name" >&2; exit 1 ;;
      create)
        name=""; runlabel=""; modelabel=""
        while [ $# -gt 0 ]; do
          case "$1" in
            --label)
              kv="$2"; shift 2
              case "$kv" in
                com.dsbot.restore.run=*)  runlabel="${kv#com.dsbot.restore.run=}" ;;
                com.dsbot.restore.mode=*) modelabel="${kv#com.dsbot.restore.mode=}" ;;
              esac ;;
            *) name="$1"; shift ;;
          esac
        done
        if [ -n "${FAKE_CREATE_FAIL:-}" ]; then echo "fake-docker: create failed" >&2; exit 1; fi
        mkdir -p "$FAKE_VOLS_DIR/$name"
        if [ -n "${FAKE_VOL_FOREIGN_LABEL:-}" ]; then
          printf 'run=foreign-run-000\nmode=foreign\n' > "$FAKE_VOLS_DIR/$name/labels"
        elif [ ! -f "$FAKE_VOLS_DIR/$name/labels" ]; then
          # реальная семантика: create на существующем volume молча НЕ трогает
          # старые метки — здесь это эмулирует ветка «labels уже есть»
          printf 'run=%s\nmode=%s\n' "$runlabel" "$modelabel" > "$FAKE_VOLS_DIR/$name/labels"
        fi
        printf '%s\n' "$name"; exit 0 ;;
      rm)
        name="${1:-}"
        rm -rf "$FAKE_VOLS_DIR/$name"
        exit 0 ;;
      ls)
        for d in "$FAKE_VOLS_DIR"/*/; do
          [ -d "$d" ] && basename "$d"
        done
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
  run)
    consume_stdin
    if [ -n "${FAKE_TAR_EXTRACT_FAIL:-}" ] && [[ "$*" == *"tar -xf"* ]]; then exit 1; fi
    exit 0 ;;
  compose)
    while [ $# -gt 0 ]; do
      case "$1" in
        -p|-f|--env-file) shift 2 ;;
        *) break ;;
      esac
    done
    sub="${1:-}"; shift || true
    case "$sub" in
      version) echo "Docker Compose version fake"; exit 0 ;;
      ps) exit 0 ;;
      exec)
        while [ $# -gt 0 ]; do case "$1" in -T) shift ;; *) break ;; esac; done
        service="${1:-}"; shift || true
        bin="${1:-}"; shift || true
        case "$bin" in
          mongosh)
            eval=""
            while [ $# -gt 0 ]; do
              if [ "$1" = "--eval" ]; then eval="$2"; shift 2; else shift; fi
            done
            if [[ "$eval" == *listDatabases* ]]; then
              # никакой интерполяции имён в JS быть не может — сверяем литерал
              if [[ "$eval" != 'db.adminCommand({listDatabases:1}).databases.map(d=>d.name).join(" ")' ]]; then
                echo "fake-docker: список БД запрошен НЕ фиксированным литералом: $eval" >&2
                exit 9
              fi
              n="$(counter listdb)"
              if [ "$n" = "1" ] && [ -n "${FAKE_DB_LIST_1:-}" ]; then
                printf '%s\n' "$FAKE_DB_LIST_1"; exit 0
              fi
              printf '%s\n' "${FAKE_DB_LIST:-admin local config}"; exit 0
            elif [[ "$eval" == *dropDatabase* ]]; then
              exit 0
            fi
            exit 0 ;;
          mongorestore)
            consume_stdin
            [ -n "${FAKE_RESTORE_FAIL:-}" ] && exit 1
            exit 0 ;;
          *) exit 0 ;;
        esac ;;
      run)
        consume_stdin
        while [ $# -gt 0 ]; do
          case "$1" in
            --rm|--no-deps|-T) shift ;;
            -e|-v) shift 2 ;;
            *) break ;;
          esac
        done
        service="${1:-}"; shift || true
        if [ "$service" = "gateway" ]; then
          [ -n "${FAKE_VERIFY_FAIL:-}" ] && exit 1
          exit 0
        fi
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
"""

FAKE_AGE = r"""#!/usr/bin/env bash
# Тестовая заглушка age: симметричный «шифр» = копия (script читает -i ключ).
case "${1:-}" in
  --version) echo "age fake"; exit 0 ;;
  --encrypt)
    shift; out=""; inp=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -i) shift 2 ;;
        -o) out="$2"; shift 2 ;;
        *) inp="$1"; shift ;;
      esac
    done
    cat "$inp" > "$out"; exit 0 ;;
  --decrypt)
    if [ -n "${FAKE_AGE_DECRYPT_FAIL:-}" ]; then
      echo "age: decryption failed: bad key" >&2; exit 1
    fi
    shift; last=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -i) shift 2 ;;
        *) last="$1"; shift ;;
      esac
    done
    if [ ! -f "$last" ]; then echo "age: file not found: $last" >&2; exit 1; fi
    cat "$last"; exit 0 ;;
esac
echo "age fake: unsupported $*" >&2; exit 1
"""

# Переменные окружения канонически приходят ЧЕРЕЗ ФАЙЛ: `wsl --exec` не
# пробрасывает Windows-переменные в WSL (проброс только через WSLENV), поэтому
# subprocess-env на Windows теряется и FAKE_DOCKER_LOG приходит пустым.
# Первый аргумент runner'а — envexports.sh с export'ами; аргументы restore.sh
# идут после него. На Linux тот же путь работает без изменений.
RUNNER_TMPL = """#!/usr/bin/env bash
set -e
source "$1"
shift
BIN="$(mktemp -d)"
cp "__FAKEDIR__/docker" "$BIN/docker"
cp "__FAKEDIR__/age" "$BIN/age"
chmod 755 "$BIN/docker" "$BIN/age"
: > "$FAKE_DOCKER_LOG"
export PATH="$BIN:$PATH"
exec bash "__RESTORE__" "$@"
"""


def make_tar(members: list[tuple[str, int]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, size in members:
            info = tarfile.TarInfo(name)
            info.size = size
            tf.addfile(info, io.BytesIO(b"z" * size))
    return buf.getvalue()


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.fakebin = tmp_path / "fakebin"
        self.fakebin.mkdir()
        (self.fakebin / "docker").write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
        (self.fakebin / "age").write_text(FAKE_AGE, encoding="utf-8", newline="\n")
        self.backup_dir = tmp_path / "backups"
        (self.backup_dir / "production").mkdir(parents=True)
        self.vols = tmp_path / "vols"
        self.vols.mkdir()
        self.log = tmp_path / "docker.log"
        self.counters = tmp_path / "counters"
        self.age_key = tmp_path / "age.key"
        self.age_key.write_text(
            "# created: 2026-09-01\n# public key: age1fakefake\nAGE-SECRET-KEY-TESTONLY111\n",
            encoding="utf-8", newline="\n")
        self.env_file = tmp_path / "prod.env"
        self.env_file.write_text("\n".join([
            f"BACKUP_DIR={psh(self.backup_dir)}",
            f"BACKUP_AGE_KEY_FILE={psh(self.age_key)}",
            "MONGO_DB=voice_tracker_production",
            "MONGO_IMAGE=fake/mongo:8.0",
            "MEDIA_VOLUME=dsbot-media",
            "DSBOT_UID=10001",
            "DSBOT_GID=10001",
        ]) + "\n", encoding="utf-8", newline="\n")
        self.state = tmp_path / "state.json"
        self.run_dir = self.make_run_point()

    def make_run_point(self, media_members: list[tuple[str, int]] | None = None) -> Path:
        rd = self.backup_dir / "production" / "dsbot-production-20260926T043000Z"
        rd.mkdir(exist_ok=True)
        members = media_members if media_members is not None else [("77/2026-09/abc.bin", 64)]
        (rd / "mongo.archive.age").write_bytes(b"fake-bson-archive-bytes")
        (rd / "media.age").write_bytes(make_tar(members))
        files = []
        for name in ("mongo.archive.age", "media.age"):
            f = rd / name
            files.append({"name": name,
                          "sha256Encrypted": backup_manifest.sha256_file(f),
                          "bytes": f.stat().st_size})
        manifest = backup_manifest.build(
            profile="production", run_id=rd.name, created_at_utc="2026-09-26T04:30:00Z",
            source_db="voice_tracker_production", schema_version=1, app_revision="t",
            counts={"totalDocs": 0, "collections": [], "schemaVersion": 1},
            media={"files": len(members), "bytes": sum(s for _, s in members)},
            consistency={"writersFrozen": True, "consistentSnapshot": True},
            tools={"mongodump": "fake"}, durations={"freezeWindowSeconds": 1},
            files=files)
        (rd / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        (rd / ".verified_ok").touch()
        return rd

    def run(self, *args: str, extra_env: dict[str, str] | None = None):
        # единственный источник значений — exports: он уходит и в subprocess-env
        # (Linux/WSL-нативный прогон), и в envexports.sh (WIN: wsl --exec env
        # не пробрасывает). Секретов здесь нет — только фейковые пути/списки.
        exports = {
            "DSBOT_ENV_FILE": psh(self.env_file),
            "FAKE_DOCKER_LOG": psh(self.log),
            "FAKE_VOLS_DIR": psh(self.vols),
            "FAKE_COUNTER_DIR": psh(self.counters),
            "FAKE_DB_LIST": "admin local config voice_tracker_production dsbot-noise",
        }
        if extra_env:
            exports.update(extra_env)
        env = dict(os.environ)
        env.update(exports)
        lines = ["export %s='%s'" % (k, str(v).replace("'", "'\\''"))
                 for k, v in exports.items()]
        env_exports = self.tmp / "envexports.sh"
        env_exports.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        base = list(args) + ["--from", psh(self.run_dir), "--state", psh(self.state)]
        runner = self.tmp / "runner.sh"
        runner.write_text(
            RUNNER_TMPL.replace("__FAKEDIR__", psh(self.fakebin))
            .replace("__RESTORE__", str(psh(DEPLOY_BACKUP / "restore.sh"))),
            encoding="utf-8", newline="\n")
        cmd = (["wsl", "--exec", "bash", psh(runner)] if WIN else ["bash", psh(runner)]
               ) + [psh(env_exports)] + base
        return subprocess.run(cmd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300,
                              stdin=subprocess.DEVNULL)

    def log_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return [ln for ln in self.log.read_text(encoding="utf-8", errors="replace").splitlines() if ln]

    def seq(self) -> list[str]:
        tokens: list[str] = []
        for ln in self.log_lines():
            if "listDatabases" in ln:
                tokens.append("list")
            elif "dropDatabase" in ln:
                tokens.append("drop")
            elif "volume create" in ln:
                tokens.append("create")
            elif "volume rm" in ln:
                tokens.append("vol-rm")
            elif "volume inspect" in ln and "com.dsbot.restore.run" in ln:
                tokens.append("label-check")
            elif "volume inspect" in ln and "Mountpoint" in ln:
                tokens.append("mount")
            elif "volume inspect" in ln:
                tokens.append("inspect")
            elif "tar -xf" in ln:
                tokens.append("extract")
            elif "ch -R" in ln:
                tokens.append("chown")
            elif "mongorestore" in ln:
                tokens.append("restore")
            elif "backup_report verify" in ln:
                tokens.append("verify")
        return tokens

    def state_json(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def live_volumes(self) -> list[str]:
        return sorted(p.name for p in self.vols.iterdir() if p.is_dir())


@pytest.fixture()
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# ---------------------------------------------------- негатив: чужие цели


def test_existing_volume_refused_before_any_write(h: Harness) -> None:
    proc = h.run("production", "--keep", extra_env={"FAKE_VOL_EXISTS_ALWAYS": "1"})
    assert proc.returncode != 0
    assert "уже существует" in (proc.stderr + proc.stdout)
    seq = h.seq()
    # ни одна запись не состоялась; чужой volume не удалён
    assert "create" not in seq and "restore" not in seq
    assert "vol-rm" not in seq and "drop" not in seq
    assert "extract" not in seq


def test_target_db_present_refused_before_writes(h: Harness) -> None:
    proc = h.run("production", "--keep", "--into-db", FIXED_DB,
                 extra_env={"FAKE_DB_LIST": f"admin local config voice_tracker_production {FIXED_DB}"})
    assert proc.returncode != 0
    seq = h.seq()
    assert seq.count("list") >= 1
    assert "create" not in seq and "restore" not in seq and "drop" not in seq


def test_db_appeared_in_race_second_gate_catches(h: Harness) -> None:
    # первый list — цели нет; все «гонки» после этого видят цель: mongorestore запрещён
    proc = h.run("production", "--into-db", FIXED_DB, extra_env={
        "FAKE_DB_LIST_1": "admin local config voice_tracker_production",
        "FAKE_DB_LIST": f"admin local config {FIXED_DB}",
    })
    assert proc.returncode != 0
    seq = h.seq()
    assert seq.count("list") == 2
    assert seq.index("create") < seq.index("list", seq.index("create"))
    assert "restore" not in seq          # второй гейт поймал гонку
    assert "extract" in seq              # media-том свой, распаковка была
    assert "drop" not in seq             # чужую (уже существующую) БД не трогаем
    assert "vol-rm" in seq               # свой volume убран
    assert h.live_volumes() == []


def test_volume_create_returning_existing_foreign_is_refused_and_untouched(h: Harness) -> None:
    # дефект R26-08: «docker volume create молча возвращает существующий».
    # Фейк на create пишет ЧУЖУЮ метку; скрипт обязан отказать после сверки
    # метки и никогда не удалять этот ресурс.
    proc = h.run("production", extra_env={"FAKE_VOL_FOREIGN_LABEL": "1"})
    assert proc.returncode != 0
    assert "создан НЕ этим прогоном" in (proc.stderr + proc.stdout)
    seq = h.seq()
    assert "create" in seq and "extract" not in seq and "restore" not in seq
    assert "vol-rm" not in seq and "drop" not in seq
    assert len(h.live_volumes()) == 1  # чужой volume цел
    st = h.state_json()
    assert st["failed"] == "volume" and st["cleaned"] is False
    assert "db-restored" not in st["reached"]


# ---------------------------------------------------- аргументы и гейты


def test_no_verify_flag_is_gone(h: Harness) -> None:
    proc = h.run("production", "--no-verify")
    assert proc.returncode != 0
    assert "неизвестный аргумент" in (proc.stderr + proc.stdout)
    assert h.log_lines() == []  # до docker вообще не доходило


def test_media_volume_flag_is_gone(h: Harness) -> None:
    proc = h.run("production", "--media-volume", "dsbot-media")
    assert proc.returncode != 0
    assert "неизвестный аргумент" in (proc.stderr + proc.stdout)
    assert h.log_lines() == []


def test_rehearsal_rejects_prod_and_injection_db_names(h: Harness) -> None:
    for bad in ("voice_tracker_production", "x';db.getSiblingDB('admin').dropDatabase()"):
        proc = h.run("production", "--into-db", bad)
        assert proc.returncode != 0, bad
        assert "restore_targets:" in (proc.stderr + proc.stdout), bad
    assert h.log_lines() == [] or all("mongorestore" not in ln for ln in h.log_lines())
    assert "restore" not in h.seq() and "create" not in h.seq()


def test_truncated_hour_targets_are_rejected_by_shape(h: Harness) -> None:
    # старый дефект ${TS:2:8}: «усечённый до часа» идентификатор больше не форма цели
    proc = h.run("production", "--into-db", "voice_tracker_production_rehearsal_26092704")
    assert proc.returncode != 0
    assert "restore_targets:" in (proc.stderr + proc.stdout)


# ---------------------------------------------------- целостность архива


def test_bad_checksum_refused_before_docker_work(h: Harness) -> None:
    victim = h.run_dir / "mongo.archive.age"
    victim.write_bytes(b"tampered-bytes-XXXXXXXXXXXXX")
    proc = h.run("production", "--keep")
    assert proc.returncode != 0
    assert "sha256" in (proc.stderr + proc.stdout)
    assert "restore" not in h.seq() and "create" not in h.seq() and "extract" not in h.seq()


def test_bad_age_key_refuses_with_no_surviving_targets(h: Harness) -> None:
    proc = h.run("production", extra_env={"FAKE_AGE_DECRYPT_FAIL": "1"})
    assert proc.returncode != 0
    seq = h.seq()
    assert "restore" not in seq and "drop" not in seq
    # свой volume был создан до отказа и обязан быть убран cleanup'ом прогона
    assert "vol-rm" in seq
    assert h.live_volumes() == []
    st = h.state_json()
    assert st["failed"] == "media" and st["cleaned"] is True


def test_tar_traversal_blocked_before_extraction(h: Harness) -> None:
    h.run_dir = h.make_run_point(media_members=[("77/ok.bin", 32), ("../evil.bin", 10)])
    proc = h.run("production")
    assert proc.returncode != 0
    assert "traversal" in (proc.stderr + proc.stdout)
    seq = h.seq()
    assert "extract" not in seq and "chown" not in seq and "restore" not in seq
    assert "vol-rm" in seq  # свой volume убран, чужие не тронуты


def test_tar_oversize_blocked_by_manifest_ceiling(h: Harness) -> None:
    # манифест занижает распакованный объём (битый/подделанный снимок):
    # потолок media.bytes*2 обязан остановить распаковку до записи в volume
    h.run_dir = h.make_run_point(media_members=[("77/big.bin", 4096)])
    mf = h.run_dir / "manifest.json"
    data = json.loads(mf.read_text(encoding="utf-8"))
    data["media"]["bytes"] = 100
    mf.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    proc = h.run("production")
    assert proc.returncode != 0
    assert "потолок" in (proc.stderr + proc.stdout)
    seq = h.seq()
    assert "extract" not in seq and "restore" not in seq
    assert "vol-rm" in seq and "drop" not in seq


# ---------------------------------------------------- crash/cleanup


def test_crash_after_db_restored_cleans_only_state_confirmed_db(h: Harness) -> None:
    proc = h.run("production", extra_env={"FAKE_VERIFY_FAIL": "1"})
    assert proc.returncode != 0
    seq = h.seq()
    assert seq.index("restore") < seq.index("verify")
    assert "drop" in seq and "vol-rm" in seq       # обе СВОИ цели удалены
    st = h.state_json()
    assert st["failed"] == "verify" and st["cleaned"] is True
    assert "db-restored" in st["reached"]          # drop разрешён именно потому
    assert h.live_volumes() == []


def test_crash_at_volume_create_leaves_no_state_confirmed_db(h: Harness) -> None:
    proc = h.run("production", "--keep", extra_env={"FAKE_CREATE_FAIL": "1"})
    assert proc.returncode != 0
    seq = h.seq()
    assert "restore" not in seq and "drop" not in seq and "vol-rm" not in seq
    assert h.state_json()["phase"] == "failed:volume"


# ---------------------------------------------------- успех / keep / resume


def test_success_rehearsal_call_sequence_without_drop(h: Harness) -> None:
    proc = h.run("production")
    assert proc.returncode == 0, proc.stderr[-2000:]
    seq = h.seq()
    first = seq.index("list")
    create = seq.index("create")
    restore = seq.index("restore")
    # гейты до записей; двойная проверка БД вокруг media-фазы; cleanup в конце
    assert first < create < restore
    assert seq.index("label-check") > create
    assert seq.index("mount") > create
    assert seq.index("extract") < restore and seq.index("chown") < restore
    second_list = seq.index("list", first + 1)
    assert second_list < restore                     # double-check гонки перед записью
    assert seq.index("verify") > restore
    assert seq.index("drop") > seq.index("verify")
    assert seq.index("vol-rm") > seq.index("drop")
    # mongorestore: ни одного --drop в команде
    restore_lines = [ln for ln in h.log_lines() if "mongorestore" in ln]
    assert restore_lines and all("--drop" not in ln for ln in restore_lines)
    assert "--nsTo" in restore_lines[0]
    # volume создан с ownership-метками прогона
    create_line = next(ln for ln in h.log_lines() if "volume create" in ln)
    assert "--label com.dsbot.restore.run=" in create_line
    assert "com.dsbot.restore.mode=rehearsal" in create_line
    # chown под UID/GID gateway из env профиля
    chown_line = next(ln for ln in h.log_lines() if "ch -R" in ln)
    assert "ch -R 10001:10001" in chown_line
    # dropDatabase — только цель, собранная из манифеста прогона
    st = h.state_json()
    drop_line = next(ln for ln in h.log_lines() if "dropDatabase" in ln)
    assert st["db"] in drop_line and st["volume"] and st["cleaned"] is True
    assert h.live_volumes() == []
    # фиксированный литерал listDatabases отработал (иначе фейк упал бы с rc 9)
    assert proc.returncode == 0


def test_keep_preserves_targets_and_state_verified(h: Harness) -> None:
    proc = h.run("production", "--keep")
    assert proc.returncode == 0, proc.stderr[-2000:]
    seq = h.seq()
    assert "drop" not in seq and "vol-rm" not in seq
    st = h.state_json()
    assert st["phase"] == "verified" and st["cleaned"] is False
    assert h.live_volumes() == [st["volume"]]


def test_rerun_on_same_state_without_resume_is_refused(h: Harness) -> None:
    assert h.run("production", "--keep").returncode == 0
    before = h.state.read_bytes()
    proc = h.run("production", "--keep")   # повтор без --resume: новый run id в чужой state
    assert proc.returncode != 0
    assert "state-файл принадлежит" in (proc.stderr + proc.stdout)
    assert h.state.read_bytes() == before  # чужой прогон не перезатёрт


def test_resume_with_matching_state_skips_completed_steps(h: Harness) -> None:
    assert h.run("production", "--keep").returncode == 0
    st1 = h.state_json()
    proc = h.run("production", "--keep", "--resume")
    assert proc.returncode == 0, proc.stderr[-2000:]
    seq = h.seq()  # лог обнуляется на каждый запуск runner'а
    assert "restore" not in seq and "extract" not in seq and "verify" not in seq
    assert "list" not in seq  # цели уже достигнуты — гейты не нужны
    st2 = h.state_json()
    assert st2["runId"] == st1["runId"] and st2["db"] == st1["db"]


# ---------------------------------------------------- cutover


def test_cutover_requires_confirm_dest(h: Harness) -> None:
    proc = h.run("production", "--mode", "cutover", "--into-db", "voice_tracker_production")
    assert proc.returncode != 0
    assert "--confirm-dest" in (proc.stderr + proc.stdout)


def test_cutover_dest_must_match_exact(h: Harness) -> None:
    proc = h.run("production", "--mode", "cutover", "--into-db", "voice_tracker_production",
                 "--confirm-dest", "voice_tracker_production_typo")
    assert proc.returncode != 0
    assert h.log_lines() == [] or "mongorestore" not in "\n".join(h.log_lines())


def test_cutover_success_keeps_targets_and_forces_keep(h: Harness) -> None:
    # чистый Linux-хост: voice_tracker_production отсутствует на сервере
    proc = h.run("production", "--mode", "cutover",
                 "--into-db", "voice_tracker_production",
                 "--confirm-dest", "voice_tracker_production",
                 extra_env={"FAKE_DB_LIST": "admin local config"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    seq = h.seq()
    assert "restore" in seq and "verify" in seq
    assert "drop" not in seq and "vol-rm" not in seq  # cleanup целей в cutover запрещён
    st = h.state_json()
    assert st["mode"] == "cutover" and st["phase"] == "verified" and st["cleaned"] is False
    assert h.live_volumes() == [st["volume"]]  # цель сохранена для активации
    out = proc.stderr + proc.stdout
    assert "mode: cutover | keep: 1" in out  # --keep форсится и попадает в отчёт
    # cutover-цель (prod-имя) никогда не отдаётся в dropDatabase-интерполяцию
    assert "dropDatabase" not in "\n".join(h.log_lines())
