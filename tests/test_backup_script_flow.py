"""R26-09 flow: РЕАЛЬНЫЙ deploy/backup/backup.sh с заглушками docker/age.

Приём harness'а — как в tests/test_restore_script_flow.py: скрипт не копируется
и не переписывается под тест; в PATH подставляются фейковые docker/age, каждый
вызов логируется. Реальные контейнеры не дёргаются: ни один вызов не уходит за
пределы фейка.

Что проверяется (декларированный дефект R26-09):
  a) happy path: stream-encryption без plaintext-стейджинга, umask 077,
     парный freeze/unfreeze, финальные имена не менялись;
  b) отказ age на media.age: стенд НЕ остаётся замороженным, .incomplete не
     становится точкой, предыдущая проверенная точка цела (B02);
  c) ENOSPC-синтетика (частичная запись .age + nonzero exit) — те же гарантии;
  d) SIGKILL прогона сразу после mongo.archive.age: plaintext дампа на диске
     не существует ни при каком исходе (stream-encrypt по построению);
  e) preflight ключа: невалидный age-ключ → отказ ДО первого compose stop;
  f/g) ops-lock: параллельный backup и backup-вовремя-restore получают отказ,
     не успев заморозить writers;
  h) R26-09 добор: prune --execute ВНЕ прогона механически берёт тот же
     ops-lock (отказ при занятом, без единого удаления), standalone-захват
     работает, dry-run/status остаются read-only; на Windows-python
     (fcntl=None) — fail-closed «требует POSIX-блокировки».

Номера вызовов age-«шифратора» в FAKE_AGE_FAIL_CALL/STALL_CALL: 1=preflight,
2=mongo.archive.age, 3=media.age (round-trip preflight тоже шифрует).

На Windows pytest гоняется python.exe → bash запускается через `wsl --exec`;
на Linux (в т.ч. внутри WSL) — напрямую. Пути конвертируются C:\\x → /mnt/c/x.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY_BACKUP = REPO / "deploy" / "backup"
RETENTION = DEPLOY_BACKUP / "backup_retention.py"
sys.path.insert(0, str(DEPLOY_BACKUP))

import backup_manifest  # noqa: E402

WIN = os.name == "nt"
BRIDGE = (shutil.which("wsl") is not None) if WIN else (shutil.which("bash") is not None)

pytestmark = pytest.mark.skipif(
    not BRIDGE,
    reason="нет bash-моста (Windows: требуется WSL с python3; Linux: bash+python3)",
)

# сырые байты «дампа» из фейкового mongodump/tar: ни в одном файле бэкапа они
# не должны появляться в открытом виде (смысл R26-09 — stream-encrypt)
MONGO_RAW = b"MONGODUMP-PAYLOAD"
TAR_RAW = b"TARSTREAM-PAYLOAD"
# старые имена plaintext-промежутков: не должны создаваться никогда
PLAINTEXT_NAMES = ("mongo.archive", "media.tar")


def psh(p: Path) -> str:
    """Путь для bash-интерпретатора: на Windows python — WSL-представление."""
    if WIN:
        s = p.as_posix()  # 'C:/Users/...'
        return f"/mnt/{s[0].lower()}/{s[2:].lstrip('/')}"
    return str(p)


FAKE_DOCKER = r"""#!/usr/bin/env bash
# Тестовая заглушка docker CLI для R26-09 (backup-конвейер). Каждый вызов
# логируется одной строкой; ответы — синтетика нужной формы (JSON counts/media,
# «поток дампа», «tar-поток»), реальная инфраструктура не затрагивается.
printf '%s\n' "docker $*" >> "$FAKE_DOCKER_LOG"

case "${1:-}" in
  info) exit 0 ;;
  -v|--version) echo "Docker version fake"; exit 0 ;;
  inspect)
    # container_label(): docker inspect -f '{{ index .Config.Labels "…revision" }}' CID
    if [[ "$*" == *org.opencontainers.image.revision* ]]; then
      echo "fakerevisioncafe1234"
    fi
    exit 0 ;;
  compose)
    shift || true
    while [ $# -gt 0 ]; do
      case "$1" in
        -p|-f|--env-file) shift 2 ;;
        *) break ;;
      esac
    done
    sub="${1:-}"; shift || true
    case "$sub" in
      version) echo "Docker Compose version fake"; exit 0 ;;
      config)
        if [[ "$*" == *--services* ]]; then
          printf 'gateway\ntracker\nweb\nmongo\nnats\n'
        fi
        exit 0 ;;
      ps)
        if [[ "$*" == *-q* ]]; then echo "fakecid"; exit 0; fi
        # ps --services — «поднятые» сервисы: freeze запоминает их для unfreeze
        printf 'gateway\ntracker\nweb\nmongo\nnats\n'
        exit 0 ;;
      stop) exit 0 ;;
      up) exit 0 ;;
      exec)
        while [ $# -gt 0 ]; do case "$1" in -T) shift ;; *) break ;; esac; done
        service="${1:-}"; shift || true
        bin="${1:-}"; shift || true
        case "$bin" in
          mongodump)
            if [[ "$*" == *--version* ]]; then echo "mongodump version fake"; exit 0; fi
            # stdout: «архив дампа» (в реальном конвейере идёт в pipe age)
            printf 'MONGODUMP-PAYLOAD-0123456789ABCDEF\n'
            exit 0 ;;
          *) exit 0 ;;
        esac ;;
      run)
        if [[ "$*" == *"--entrypoint tar"* ]]; then
          printf 'TARSTREAM-PAYLOAD-0123456789ABCDEF\n'
          exit 0
        fi
        if [[ "$*" == *"backup_report counts"* ]]; then
          printf '{"db": "voice_tracker_production", "totalDocs": 3, "collections": [], "schemaVersion": 1}\n'
          exit 0
        fi
        if [[ "$*" == *"backup_report media"* ]]; then
          printf '{"files": 1, "bytes": 27}\n'
          exit 0
        fi
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
"""

FAKE_AGE = r"""#!/usr/bin/env bash
# Тестовая заглушка age для R26-09. «Шифрование» = base64: строгий обратимый
# round-trip (префлайт проверяет encrypt+decrypt побайтово) и при этом сырые
# байты дампа в шифротексте не встречаются (base64-алфавит не содержит «-»),
# поэтому тесты «нет plaintext» не могут пройти ложно.
# Режимы (env): FAKE_AGE_MODE = ok|decrypt-fail|roundtrip-break|enospc,
# FAKE_AGE_FAIL_CALL=N — падение N-го --encrypt, FAKE_AGE_STALL_CALL=N —
# N-й --encrypt создаёт -o и «висит» (для SIGKILL-проверки).
printf '%s\n' "age $*" >> "$FAKE_AGE_LOG"

counter() {
  local f="$FAKE_AGE_COUNTER_DIR/$1" n=0
  mkdir -p "$FAKE_AGE_COUNTER_DIR"
  if [ -f "$f" ]; then n="$(cat "$f")"; fi
  n=$((n+1)); printf '%s' "$n" > "$f"; printf '%s' "$n"
}

mode="${FAKE_AGE_MODE:-ok}"
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
    n="$(counter encrypt)"
    if [ -n "${FAKE_AGE_STALL_CALL:-}" ] && [ "$n" = "$FAKE_AGE_STALL_CALL" ]; then
      : > "$out"   # «шифрование пошло», процесс висит — тест убьёт прогон снаружи
      sleep "${FAKE_AGE_STALL_SECS:-300}"
      exit 0
    fi
    if [ -n "${FAKE_AGE_FAIL_CALL:-}" ] && [ "$n" = "$FAKE_AGE_FAIL_CALL" ]; then
      if [ "$mode" = enospc ]; then
        # «диск кончился»: частичный шифротекст записан, exit ненулевой
        if [ -n "$inp" ]; then head -c 8 "$inp" | base64 > "$out"; else head -c 8 | base64 > "$out"; fi
      fi
      echo "age fake: encrypt call #$n failed (simulated)" >&2
      exit 1
    fi
    if [ -n "$inp" ]; then base64 < "$inp" > "$out"; else base64 > "$out"; fi
    exit 0 ;;
  --decrypt)
    shift; last=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -i) shift 2 ;;
        *) last="$1"; shift ;;
      esac
    done
    if [ ! -f "$last" ]; then echo "age fake: file not found: $last" >&2; exit 1; fi
    case "$mode" in
      decrypt-fail) echo "age: decryption failed: bad key" >&2; exit 1 ;;
      roundtrip-break)
        # валидный на вид вывод, но НЕ побайтово-обратный: префлайт обязан
        # ловить именно рассогласование encrypt/decrypt одним ключом
        { base64 -d < "$last"; printf 'X'; }
        exit 0 ;;
    esac
    base64 -d < "$last"
    exit 0 ;;
esac
echo "age fake: unsupported $*" >&2; exit 1
"""

# Переменные окружения канонически приходят ЧЕРЕЗ ФАЙЛ (см. комментарий в
# tests/test_restore_script_flow.py): wsl --exec не пробрасывает env Windows→WSL.
RUNNER_TMPL = """#!/usr/bin/env bash
set -e
source "$1"
shift
BIN="$(mktemp -d)"
cp "__FAKEDIR__/docker" "$BIN/docker"
cp "__FAKEDIR__/age" "$BIN/age"
chmod 755 "$BIN/docker" "$BIN/age"
: > "$FAKE_DOCKER_LOG"
: > "$FAKE_AGE_LOG"
export PATH="$BIN:$PATH"
exec bash "__SCRIPT__" "$@"
"""


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.fakebin = tmp_path / "fakebin"
        self.fakebin.mkdir()
        (self.fakebin / "docker").write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
        (self.fakebin / "age").write_text(FAKE_AGE, encoding="utf-8", newline="\n")
        # ВАЖНО: BACKUP_DIR намеренно НЕ предсоздаётся — каталог должен
        # появиться сам под umask 077 из скрипта, иначе проверка прав смысла
        # не имеет (python mkdir дал бы 755).
        self.backup_dir = tmp_path / "backups"
        self.log = tmp_path / "docker.log"
        self.age_log = tmp_path / "age.log"
        self.counters = tmp_path / "age-counters"
        self.age_key = tmp_path / "age.key"
        self.age_key.write_text(
            "# created: 2026-09-28\n# public key: age1fakefake\nAGE-SECRET-KEY-TESTONLY111\n",
            encoding="utf-8", newline="\n")
        self.env_file = tmp_path / "prod.env"
        self.env_file.write_text("\n".join([
            f"BACKUP_DIR={psh(self.backup_dir)}",
            f"BACKUP_AGE_KEY_FILE={psh(self.age_key)}",
            "MONGO_DB=voice_tracker_production",
            "MONGO_IMAGE=fake/mongo:8.0",
            "MONGO_BACKUP_URI=mongodb://dsbot_backup:" "fake-backup-pass" "@127.0.0.1:27017/?authSource=voice_tracker_production",
            "MONGO_ADMIN_URI=mongodb://dsbot_root:" "fake-root-pass" "@127.0.0.1:27017/admin?authSource=admin",
            "MONGO_RESTORE_URI=mongodb://dsbot_restore:" "fake-restore-pass" "@127.0.0.1:27017/?authSource=voice_tracker_production",
            "RETENTION_DAILY=7",
            "RETENTION_WEEKLY=4",
        ]) + "\n", encoding="utf-8", newline="\n")
        self.state = tmp_path / "restore-state.json"

    # ---------- запуск реальных скриптов ----------
    def _prepare(self, script: str, args: list[str],
                 extra_env: dict[str, str] | None) -> tuple[list[str], dict[str, str]]:
        exports = {
            "DSBOT_ENV_FILE": psh(self.env_file),
            "FAKE_DOCKER_LOG": psh(self.log),
            "FAKE_AGE_LOG": psh(self.age_log),
            "FAKE_AGE_COUNTER_DIR": psh(self.counters),
        }
        if extra_env:
            exports.update(extra_env)
        env = dict(os.environ)
        env.update(exports)
        lines = ["export %s='%s'" % (k, str(v).replace("'", "'\\''"))
                 for k, v in exports.items()]
        env_exports = self.tmp / "envexports.sh"
        env_exports.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        runner = self.tmp / f"runner-{script}"
        runner.write_text(
            RUNNER_TMPL.replace("__FAKEDIR__", psh(self.fakebin))
            .replace("__SCRIPT__", psh(DEPLOY_BACKUP / script)),
            encoding="utf-8", newline="\n")
        cmd = (["wsl", "--exec", "bash", psh(runner)] if WIN else ["bash", psh(runner)]) \
            + [psh(env_exports)] + args
        return cmd, env

    def run(self, args: list[str], extra_env: dict[str, str] | None = None,
            script: str = "backup.sh") -> subprocess.CompletedProcess:
        cmd, env = self._prepare(script, args, extra_env)
        return subprocess.run(cmd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300,
                              stdin=subprocess.DEVNULL)

    def run_backup(self, *args: str, extra_env: dict[str, str] | None = None):
        return self.run(["production", *args], extra_env=extra_env)

    def popen_backup(self, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
        cmd, env = self._prepare("backup.sh", ["production"], extra_env)
        # новая сессия = новый process group: SIGKILL забирает и скрипт, и
        # зависнувший фейк age (exec в runner сохраняет pid)
        return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)

    def lock_holder(self, lock: Path) -> subprocess.Popen:
        """Внешний владелец ops-lock (эмуляция «другой прогон уже идёт»)."""
        body = 'mkdir -p "$(dirname "$1")"; exec 9>"$1"; flock -x 9; echo READY >&2; sleep 300'
        cmd = (["wsl", "--exec", "bash", "-c", body, "holder", psh(lock)] if WIN
               else ["bash", "-c", body, "holder", psh(lock)])
        holder = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
        assert holder.stderr is not None
        if holder.stderr.readline().strip() != "READY":
            holder.kill()
            raise AssertionError("_holder не смог взять flock на_локе")
        return holder

    # ---------- наблюдение ----------
    def log_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return [ln for ln in self.log.read_text(encoding="utf-8", errors="replace").splitlines() if ln]

    def age_log_lines(self) -> list[str]:
        if not self.age_log.exists():
            return []
        return [ln for ln in self.age_log.read_text(encoding="utf-8", errors="replace").splitlines() if ln]

    def stop_calls(self) -> list[str]:
        return [ln for ln in self.log_lines() if re.search(r"\bcompose\b.*\bstop\b", ln)]

    def up_calls(self) -> list[str]:
        return [ln for ln in self.log_lines() if re.search(r"\bcompose\b.*\bup -d\b", ln)]

    def dump_calls(self) -> list[int]:
        return [i for i, ln in enumerate(self.log_lines())
                if "mongodump" in ln and "--archive" in ln]

    def tar_calls(self) -> list[int]:
        return [i for i, ln in enumerate(self.log_lines()) if "--entrypoint tar" in ln]

    def dest(self) -> Path:
        return self.backup_dir / "production"

    def _point_dirs(self) -> list[Path]:
        if not self.dest().is_dir():
            return []
        return sorted(d for d in self.dest().iterdir()
                      if d.is_dir() and d.name.startswith("dsbot-production-"))

    def finals(self) -> list[Path]:
        return [d for d in self._point_dirs() if not d.name.endswith(".incomplete")]

    def partials(self) -> list[Path]:
        return [d for d in self._point_dirs() if d.name.endswith(".incomplete")]

    def ops_lock(self) -> Path:
        return self.backup_dir / ".ops-production.lock"

    def snapshot(self, d: Path) -> dict[str, tuple[str | None, int, int]]:
        """Снимок каталога: содержимое + mtime + режим каждой записи (B02)."""
        out: dict[str, tuple[str | None, int, int]] = {}
        st = d.stat()
        out["."] = (None, st.st_mtime_ns, st.st_mode)
        for p in sorted(d.rglob("*")):
            s = p.stat()
            out[str(p.relative_to(d))] = (
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None,
                s.st_mtime_ns, s.st_mode)
        return out

    def assert_no_plaintext_anywhere(self) -> None:
        """Ни старого имени промежуточного файла, ни сырых байт дампа в BACKUP_DIR."""
        if not self.backup_dir.exists():
            return
        for p in self.backup_dir.rglob("*"):
            if not p.is_file():
                continue
            assert p.name not in PLAINTEXT_NAMES, f"plaintext-имя на диске: {p}"
            data = p.read_bytes()
            for marker in (MONGO_RAW, TAR_RAW):
                assert marker not in data, f"plaintext-утечка {marker!r} в {p}"

    def assert_root_layout(self) -> None:
        """Вне каталогов точек — только сам профиль и скрытый ops-lock."""
        if not self.backup_dir.exists():
            return
        for p in self.backup_dir.iterdir():
            assert p.name == "production" or p.name.startswith(".ops-"), p
        for p in self.dest().iterdir():
            assert p.is_dir() and p.name.startswith("dsbot-production-"), p

    def assert_private_perms(self) -> None:
        # На Windows tmp_path живёт на NTFS (/mnt/c в WSL — DrvFs с Windows ACL):
        # биты POSIX-прав там всегда 777 независимо от umask, проверка была бы
        # ложно-падающей. Реально права проверяются на Linux (CI ubuntu-latest).
        if WIN:
            return
        for p in [self.backup_dir, *self.backup_dir.rglob("*")]:
            assert p.stat().st_mode & 0o077 == 0, f"world-readable: {p} ({oct(p.stat().st_mode)})"

    # ---------- предпосылки ----------
    def seed_prior_point(self) -> Path:
        """Предыдущая проверенная точка (B02: прогон обязан оставить её в покое)."""
        rd = self.dest()
        rd.mkdir(parents=True, exist_ok=True)
        point = rd / "dsbot-production-20250101T000000Z"
        point.mkdir()
        (point / "mongo.archive.age").write_bytes(b"cHJpb3ItbW9uZ28tZHVtcAo=")
        (point / "media.age").write_bytes(b"cHJpb3ItbWVkaWEtdGFyCg==")
        files = [{"name": n,
                  "sha256Encrypted": backup_manifest.sha256_file(point / n),
                  "bytes": (point / n).stat().st_size}
                 for n in ("mongo.archive.age", "media.age")]
        manifest = backup_manifest.build(
            profile="production", run_id=point.name, created_at_utc="2025-01-01T00:00:00Z",
            source_db="voice_tracker_production", schema_version=1, app_revision="t",
            counts={"totalDocs": 0, "collections": [], "schemaVersion": 1},
            media={"files": 1, "bytes": 64},
            consistency={"writersFrozen": True, "consistentSnapshot": True},
            tools={"mongodump": "fake"}, durations={"freezeWindowSeconds": 1},
            files=files)
        (point / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        (point / ".verified_ok").touch()
        return point

    @staticmethod
    def wait_until(cond, timeout: float = 30.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            time.sleep(0.05)
        raise AssertionError(f"таймаут ожидания: {what}")


@pytest.fixture()
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# ---------------------------------------------------------------- статика R26-09


def test_backup_sh_umask_precedes_first_mkdir() -> None:
    """(1) umask 077 — исполняемая строка ДО первого mkdir в backup.sh и restore.sh."""
    for name in ("backup.sh", "restore.sh"):
        lines = (DEPLOY_BACKUP / name).read_text(encoding="utf-8").splitlines()
        u = next((i for i, ln in enumerate(lines) if ln.strip().startswith("umask 077")), None)
        m = next((i for i, ln in enumerate(lines)
                  if "mkdir" in ln and not ln.lstrip().startswith("#")), None)
        assert u is not None, name
        assert m is None or u < m, f"{name}: mkdir раньше umask (mkdir@{m}, umask@{u})"


def test_ops_lock_wired_into_backup_and_restore() -> None:
    """(4) хелпер acquire_ops_lock определён в _backup_common.sh (fail-closed на flock)
    и вызывается из backup.sh и restore.sh исполняемыми строками."""
    common = (DEPLOY_BACKUP / "_backup_common.sh").read_text(encoding="utf-8")
    assert "acquire_ops_lock()" in common
    assert "flock -x -n" in common
    assert "command -v flock" in common  # нет flock — отказ, не запуск без lock
    for name in ("backup.sh", "restore.sh"):
        code = "\n".join(ln for ln in (DEPLOY_BACKUP / name).read_text(encoding="utf-8").splitlines()
                         if not ln.lstrip().startswith("#"))
        assert re.search(r"^acquire_ops_lock\b", code, re.MULTILINE), name


def test_pipeline_has_no_plaintext_staging_calls() -> None:
    """(2) в backup.sh не осталось пост-шифрования файлов и rm plaintext-промежутков;
    финальные имена (mongo.archive.age/media.age) — те же, что ожидает restore.sh;
    оба снимка — конвейером в age (stdout пайпа, не редирект в plaintext-файл)."""
    code = "\n".join(ln for ln in (DEPLOY_BACKUP / "backup.sh").read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "age_encrypt" not in code
    assert '"$PARTIAL/mongo.archive"' not in code
    assert '"$PARTIAL/media.tar"' not in code
    for final in ("mongo.archive.age", "media.age"):
        assert final in code, final
    common = (DEPLOY_BACKUP / "_backup_common.sh").read_text(encoding="utf-8")
    assert "age_encrypt()" not in common  # симметричный стрим — единственный путь шифрования
    assert "--archive >" not in common    # mongodump больше не пишет plaintext-файл
    assert re.search(r"--archive \\\n\s*\| age --encrypt", common), common
    assert re.search(r"-C /data/media \. \\\n\s*\| age --encrypt", common), common


# ---------------------------------------------------------------- a) happy path


def test_happy_path_stream_encrypt_freeze_perms(h: Harness) -> None:
    """(a) успех: только шифротекст с финальными именами, .verified_ok, парный
    freeze/unfreeze вокруг дампов, права без world-битов, layout без мусора."""
    proc = h.run_backup()
    assert proc.returncode == 0, proc.stderr[-2000:]
    finals = h.finals()
    assert len(finals) == 1
    final = finals[0]
    assert (final / ".verified_ok").is_file()
    assert {p.name for p in final.iterdir()} == \
        {"mongo.archive.age", "media.age", "manifest.json", ".verified_ok"}
    assert h.partials() == []  # финализация доведена, orphan'ов нет
    h.assert_no_plaintext_anywhere()
    h.assert_root_layout()
    h.assert_private_perms()
    # вызовы age: 1=preflight, 2=mongo, 3=media; decrypt только в preflight
    enc = [ln for ln in h.age_log_lines() if ln.startswith("age --encrypt")]
    dec = [ln for ln in h.age_log_lines() if ln.startswith("age --decrypt")]
    assert len(enc) == 3 and len(dec) == 1
    # freeze/unfreeze парные и ОБОРАЧИВАЮТ оба дампа (окно нулевой записи)
    assert h.stop_calls() and h.up_calls()
    log = h.log_lines()
    stop_i = min(i for i, ln in enumerate(log) if re.search(r"\bcompose\b.*\bstop\b", ln))
    up_i = max(i for i, ln in enumerate(log) if re.search(r"\bcompose\b.*\bup -d\b", ln))
    events = h.dump_calls() + h.tar_calls()
    assert events and all(stop_i < i < up_i for i in events), \
        "дампы должны идти внутри окна заморозки"


# ------------------------------------------------- b/c) отказ age на media.age


def _assert_failed_media_run(h: Harness, prior: Path, before: dict) -> None:
    # стенд был заморожен и ОБЯЗАТЕЛЬНО разморожен (trap unfreeze)
    assert h.stop_calls(), "freeze должен был состояться"
    assert h.up_calls(), "отказ не должен оставлять writers остановленными"
    # полуточка не финализирована, предыдущая точка цела (B02), retention не тронул
    assert h.finals() == [prior]
    parts = h.partials()
    assert len(parts) == 1
    for d in parts:
        assert not (d / ".verified_ok").exists()
        for name in PLAINTEXT_NAMES:
            assert not (d / name).exists(), f"{d}/{name}"
    h.assert_no_plaintext_anywhere()
    assert h.snapshot(prior) == before


@pytest.mark.parametrize("mode", ["ok", "enospc"], ids=["age-fail", "age-enospc"])
def test_media_age_failure_unfreezes_and_keeps_prior_point(h: Harness, mode: str) -> None:
    """(b/c) шифрование mongo прошло (call 2), а media.age (call 3) падает —
    в ok-режиме сразу, в enospc — после частичной записи .age. Гарантии те же:
    ненулевой exit, unfreeze, полуточка не точка, plaintext нет, предпрогон цел."""
    prior = h.seed_prior_point()
    before = h.snapshot(prior)
    proc = h.run_backup(extra_env={"FAKE_AGE_MODE": mode, "FAKE_AGE_FAIL_CALL": "3"})
    assert proc.returncode != 0
    _assert_failed_media_run(h, prior, before)


# ---------------------------------------------------------------- d) SIGKILL


@pytest.mark.skipif(WIN, reason="SIGKILL-группы проверяем только на Linux/WSL")
def test_sigkill_after_mongo_age_leaves_no_plaintext(h: Harness) -> None:
    """(d) прогон убит сразу после появления mongo.archive.age: trap не отработал —
    и тем не менее plaintext-стейджинга нет НИКАКОГО (стрим в age по построению),
    FINAL не создан, .verified_ok нет, предыдущая точка цела."""
    prior = h.seed_prior_point()
    before = h.snapshot(prior)
    proc = h.popen_backup(extra_env={"FAKE_AGE_STALL_CALL": "3"})
    try:
        h.wait_until(lambda: h.partials(), what="каталог .incomplete")
        part = h.partials()[0]
        h.wait_until(lambda: (part / "mongo.archive.age").exists(),
                     what="mongo.archive.age до убийства")
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=30)
    assert proc.returncode != 0
    parts = h.partials()
    assert len(parts) == 1 and parts[0] == part
    # шифротекст mongo на месте, plaintext-имён нет — ни в .incomplete, ни где-либо
    assert (part / "mongo.archive.age").is_file()
    for name in PLAINTEXT_NAMES:
        assert not (part / name).exists()
    assert not (part / ".verified_ok").exists()
    assert h.finals() == [prior]
    h.assert_no_plaintext_anywhere()
    assert h.snapshot(prior) == before


# ------------------------------------------------------------- e) preflight


@pytest.mark.parametrize("mode", ["decrypt-fail", "roundtrip-break"])
def test_preflight_bad_key_refuses_before_freeze(h: Harness, mode: str) -> None:
    """(e) ключ не читается/не обратим: отказ ДО первого compose stop — стенд
    не ложится; точка не заведена; ни одного дампа не было."""
    proc = h.run_backup(extra_env={"FAKE_AGE_MODE": mode})
    assert proc.returncode != 0
    assert "preflight" in (proc.stderr + proc.stdout)
    assert h.stop_calls() == [], "preflight обязан падать до freeze writers"
    assert not any("--archive" in ln for ln in h.log_lines()), "дампов быть не должно"
    assert h.finals() == [] and h.partials() == []


def test_non_age_key_file_refuses_before_freeze(h: Harness) -> None:
    """(e) ключевым файл не является вовсе (нет AGE-SECRET-KEY-…): load_backup_env
    отказывает ещё раньше — и тоже до первого compose stop."""
    h.age_key.write_text("not-a-key\n", encoding="utf-8", newline="\n")
    proc = h.run_backup()
    assert proc.returncode != 0
    assert "AGE-SECRET-KEY" in (proc.stderr + proc.stdout)
    assert h.stop_calls() == []
    assert h.finals() == [] and h.partials() == []


# ---------------------------------------------------------------- f/g) lock


def test_parallel_backup_refused_while_lock_held(h: Harness) -> None:
    """(f) scheduler повторился, пока предыдущий прогон держит ops-lock:
    отказ с «lock» в stderr, НЕ трогая writers (ни одного compose stop)."""
    prior = h.seed_prior_point()
    before = h.snapshot(prior)
    holder = h.lock_holder(h.ops_lock())
    try:
        proc = h.run_backup()
        assert proc.returncode != 0
        assert "lock" in proc.stderr
        assert h.stop_calls() == []
        assert h.partials() == [], "отказ должен случиться до создания полуточки"
        assert h.snapshot(prior) == before
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_backup_refused_while_restore_holds_lock(h: Harness) -> None:
    """(g) backup во время restore: restore.sh берёт тот же ops-lock — здесь
    lock удержан извне (как это делает restore), backup обязан отказаться до
    freeze; сам restore при занятом локе тоже отказывает немедленно (R26-08
    гейты и state-файл не должны быть тронуты)."""
    holder = h.lock_holder(h.ops_lock())
    try:
        backup = h.run_backup()
        assert backup.returncode != 0 and "lock" in backup.stderr
        assert h.stop_calls() == []
        restore = h.run(["production", "--state", psh(h.state)], script="restore.sh")
        assert restore.returncode != 0 and "lock" in restore.stderr
        assert not h.state.exists(), "отказ по lock — до любых записей restore"
    finally:
        holder.kill()
        holder.wait(timeout=30)


# ------------------------------------------------- h) механический lock в prune


def _prune_argv(dest: Path, *, execute: bool, posix_python: bool) -> list[str]:
    """Команда CLI-запуска prune. posix_python=True — интерпретатор с fcntl:
    на Windows это python3 внутри WSL (тот же мост `wsl --exec`, что и для
    скриптов; на win python модуля fcntl нет в природе) и пути конвертируются
    в /mnt/…; для прямого Windows-python пути остаются нативными (str)."""
    if not WIN:
        python = [sys.executable]
        topath = str
    elif posix_python:
        python = ["wsl", "--exec", "python3"]
        topath = psh
    else:
        python = [sys.executable]
        topath = str
    cmd = python + [topath(RETENTION), "prune", "--dest", topath(dest),
                    "--profile", "production", "--daily-keep", "0", "--weekly-keep", "0"]
    if execute:
        cmd.append("--execute")
    return cmd


def _run_prune(dest: Path, *, execute: bool = True, posix_python: bool = True
               ) -> subprocess.CompletedProcess:
    return subprocess.run(_prune_argv(dest, execute=execute, posix_python=posix_python),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, stdin=subprocess.DEVNULL)


def _seed_newer_verified_point(h: Harness) -> Path:
    """Свежая проверенная точка (2099) — с ней план prune при keep=0/0 обязан
    удалить предпрогонную seed_prior_point: отказ «не удалил» становится
    осмысленным, а не тавтологией «удалять всё равно нечего»."""
    newer = h.dest() / "dsbot-production-20990101T000000Z"
    newer.mkdir()
    (newer / ".verified_ok").touch()
    return newer


def test_prune_cli_lock_path_mirrors_bash_helper() -> None:
    """(h-статика) путь lock в python обязан совпадать с формулой bash-хелпера
    («$BACKUP_DIR/.ops-$PROFILE.lock» при --dest="$BACKUP_DIR/$PROFILE"),
    и зеркало пути задокументировано комментарием в обоих файлах."""
    import backup_retention
    got = backup_retention.ops_lock_path(Path("/srv/backups/production"))
    assert got == Path("/srv/backups/.ops-production.lock")
    py = RETENTION.read_text(encoding="utf-8")
    common = (DEPLOY_BACKUP / "_backup_common.sh").read_text(encoding="utf-8")
    assert 'f".ops-{dest.name}.lock"' in py
    assert '"$BACKUP_DIR/.ops-$PROFILE.lock"' in common
    assert "ops_lock_path" in common  # перекрёстная ссылка в комментарии хелпера
    assert "_backup_common.sh" in py


def test_prune_execute_refused_while_ops_lock_held(h: Harness) -> None:
    """(h) retention-prune ВНЕ прогона (ручной запуск оператором) при занятом
    ops-lock обязан отказать ненулевым выходом со словом «lock» и НЕ удалить
    ни одной точки; dry-run (без --execute) остаётся read-only и работает."""
    prior = h.seed_prior_point()
    newer = _seed_newer_verified_point(h)
    before = h.snapshot(prior)
    holder = h.lock_holder(h.ops_lock())
    try:
        proc = _run_prune(h.dest())
        assert proc.returncode != 0
        assert "lock" in (proc.stderr + proc.stdout)
        assert h.snapshot(prior) == before, "отказ по lock — до любых удалений"
        assert newer.is_dir()
        dry = _run_prune(h.dest(), execute=False)
        assert dry.returncode == 0, dry.stderr
        assert "delete" in dry.stdout  # план показывается, файлы не тронуты
        assert h.snapshot(prior) == before and newer.is_dir()
    finally:
        holder.kill()
        holder.wait(timeout=30)


@pytest.mark.skipif(not WIN,
                    reason="ветка fcntl=None специфична для Windows-python (хосты бэкапа — Linux/WSL)")
def test_prune_execute_fail_closed_without_posix_fcntl(h: Harness) -> None:
    """(h-win) prune --execute под Windows python (fcntl отсутствует):
    fail-closed «требует POSIX-блокировки», ненулевой выход, удалений нет."""
    prior = h.seed_prior_point()
    newer = _seed_newer_verified_point(h)
    before = h.snapshot(prior)
    proc = _run_prune(h.dest(), posix_python=False)  # sys.executable == python.exe
    assert proc.returncode != 0
    assert "POSIX" in (proc.stderr + proc.stdout)
    assert "lock" in (proc.stderr + proc.stdout)
    assert h.snapshot(prior) == before
    assert newer.is_dir()


@pytest.mark.skipif(WIN, reason="POSIX-захват без наследника проверяем на Linux python")
def test_prune_execute_standalone_acquires_ops_lock(h: Harness) -> None:
    """(h) одиночный prune --execute (никто локе не держит): скрипт сам создаёт
    и берёт ops-lock, план исполняется — старая точка удалена, последняя
    проверенная защищена (инварианты п.8 не сломаны гейтом)."""
    prior = h.seed_prior_point()
    newer = _seed_newer_verified_point(h)
    assert not h.ops_lock().exists()
    proc = _run_prune(h.dest())
    assert proc.returncode == 0, proc.stderr
    assert h.ops_lock().is_file(), "захват создаёт тот же lock-файл, что и bash-хелпер"
    assert not prior.exists(), "план при keep=0/0 обязан удалить хвост"
    assert newer.is_dir(), "последняя проверенная точка защищена всегда"
