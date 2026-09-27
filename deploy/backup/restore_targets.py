#!/usr/bin/env python3
"""R26-08: гейты безопасного restore — имена целей, tar, run id, state прогона.

stdlib-only (host-side python3). restore.sh вызывает это ДО любой записи в
цели; каждое решение — отдельный exit-код: 0 = ок, 2 = отказ. Сообщения не
содержат секретов (имена целей/файлов секретом не являются).

Субкоманды:
  validate-db   — строгий allowlist имени целевой БД (+ сверка с source/configured)
  assert-absent — цель уже существует на сервере → отказ до первых записей
  check-tar     — media-архив: без traversal/ссылок/спец-файлов и в потолок размера
  run-id        — полная временная точность + энтропия (без усечения до часа)
  state-write   — атомарный JSON state прогона (ownership/фазы/resume)
  state-read    — одно поле state-файла (для bash-веток resume)
  state-check   — подтверждение «эту цель создал ЭТОТ прогон» (фаза+имена)

Фазы ( monotonic «reached» ): prepared → media-extracted → db-restored →
verified → cleaned; сбой пишется как failed:<этап> (диагностика не затирается).
Порядок db/media в рангах повторяет фактический поток restore.sh: volume и
media готовятся до mongorestore (шаги 4–6), поэтому db-restored «дальше»
media-extracted — именно presence db-restored в reached подтверждает право
cleanup на dropDatabase.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

RESTORE_STATE_VERSION = "dsbot-restore-state/v1"

# Канон проекта для имён rehearsal-целей: префикс voice_tracker_ обязателен
# (совместим со стендовым stand_guard-паттерном), суффикс — run id полной
# точности. В именах физически не может появиться кавычка/точка с запятой,
# поэтому интерполяция имени, ПРОШЕДШЕГО этот regex, в mongosh --eval
# безопасна — это зафиксированный приём для dropDatabase в restore.sh.
DB_ALLOWLIST_PATTERN = r"^voice_tracker_(production|staging)_rehearsal_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{6}$"
DB_ALLOWLIST_RE = re.compile(DB_ALLOWLIST_PATTERN)

# символы, допустимые для ANY целевого имени БД (cutover принимает прод-имя,
# сверяя его с --confirm-dest; regex выше на него не распространяется)
DB_SAFESTRING_RE = re.compile(r"^[A-Za-z0-9_]+$")

PHASE_RANK = {"prepared": 1, "media-extracted": 2, "db-restored": 3, "verified": 4}
TERMINAL_PHASES = {"cleaned"}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _fail(msg: str) -> int:
    print(f"restore_targets: {msg}", file=sys.stderr)
    return 2


# --------------------------------------------------------------- validate-db


def cmd_validate_db(args: argparse.Namespace) -> int:
    configured = [c for c in (args.configured or "").split(",") if c]
    db = args.db
    if not DB_ALLOWLIST_RE.match(db):
        return _fail(
            f"имя цели {db!r} не проходит rehearsal-allowlist "
            "(ожидается voice_tracker_(production|staging)_rehearsal_YYYYMMDDTHHMMSSZ_hex6; "
            "имена source/app/admin/local/config и любые другие форматы запрещены)"
        )
    if db == args.source:
        return _fail("имя цели совпадает с source БД манифеста — живой destination запрещён")
    if db in configured:
        return _fail("имя цели совпадает с настроенной (configured) БД — живой destination запрещён")
    print(f"validate-db: OK {db}")
    return 0


# --------------------------------------------------------------- assert-absent


def cmd_assert_absent(args: argparse.Namespace) -> int:
    exists = args.exists.strip().lower()
    if exists in ("1", "true", "yes"):
        return _fail("цель уже существует на сервере — прогон пишет только в отсутствующие цели")
    if exists not in ("0", "false", "no"):
        return _fail(f"assert-absent: неразобрано значение --exists {args.exists!r} (ожидается true/false)")
    print("assert-absent: OK (цель отсутствует)")
    return 0


# --------------------------------------------------------------- check-tar


def _is_unsafe_name(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or _DRIVE_RE.match(name):
        return True
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return ".." in parts


def check_tar(path: Path, max_bytes: int) -> tuple[bool, str]:
    """Проверка РАСПАКОВАННОГО tar: ни traversal, ни ссылок/спец-записей, размер в потолке."""
    total = 0
    files = 0
    try:
        with tarfile.open(name=str(path), mode="r:*") as tf:
            for member in tf:
                if _is_unsafe_name(member.name):
                    return False, f"traversal/абсолютный путь в архиве: {member.name!r}"
                if not (member.isfile() or member.isdir()):
                    # symlink/hardlink/char/block/fifo — всё, чем можно записать наружу
                    return False, f"не-regular запись в архиве: {member.name!r} (type={member.type!r})"
                if member.isfile():
                    files += 1
                    total += member.size
                    if total > max_bytes:
                        return False, f"распакованный размер превысил потолок {max_bytes} bytes"
    except tarfile.TarError as exc:
        return False, f"архив не читается как tar: {exc}"
    if total > max_bytes:
        return False, f"распакованный размер {total} превысил потолок {max_bytes} bytes"
    return True, f"check-tar: OK ({files} файлов, {total} bytes <= {max_bytes})"


def cmd_check_tar(args: argparse.Namespace) -> int:
    path = Path(args.archive)
    if not path.is_file():
        return _fail(f"archive missing: {args.archive}")
    ok, msg = check_tar(path, args.max_bytes)
    print(msg)
    return 0 if ok else 2


# --------------------------------------------------------------- run-id


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{secrets.token_hex(3)}"


def cmd_run_id(_args: argparse.Namespace) -> int:
    print(new_run_id())
    return 0


# --------------------------------------------------------------- state


def _load_state(state: Path) -> dict[str, Any] | None:
    if not state.exists():
        return None
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except ValueError:
        raise ValueError("state-файл повреждён или не JSON") from None
    if not isinstance(data, dict):
        raise ValueError("state-файл повреждён или не JSON")
    return data


def _rank_of(phase: str) -> int:
    return PHASE_RANK.get(phase, 0)


def _atomic_write(state: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(state.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, state)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cmd_state_write(args: argparse.Namespace) -> int:
    state = Path(args.state)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    phase = args.phase
    if phase not in PHASE_RANK and phase not in TERMINAL_PHASES and not phase.startswith("failed:"):
        return _fail(f"неизвестная фаза {phase!r}")
    try:
        existing = _load_state(state)
    except ValueError as exc:
        return _fail(f"{state}: {exc} (не перезаписываем — диагностика предыдущего прогона)")
    if existing is not None:
        if existing.get("runId") != args.run_id:
            return _fail(
                "state-файл принадлежит ДРУГОМУ прогону "
                f"(runId={existing.get('runId')!r}) — повторный запуск с тем же --state "
                "без --resume запрещён; укажите новый --state или удалите завершённый файл"
            )
        if existing.get("db", "") != args.db or existing.get("volume", "") != (args.volume or ""):
            return _fail("цели прогона (db/volume) не совпадают со state — resume не туда")
        reached = [p for p in existing.get("reached", []) if p in PHASE_RANK]
        max_rank = max((_rank_of(p) for p in reached), default=0)
        if phase in PHASE_RANK and _rank_of(phase) < max_rank:
            return _fail(f"фаза {phase!r} регрессирует относительно reached={reached}")
        if existing.get("cleaned") and phase not in TERMINAL_PHASES:
            return _fail("прогон уже cleaned — новые фазы не пишутся")
        data = existing
    else:
        data = {
            "restoreStateVersion": RESTORE_STATE_VERSION,
            "runId": args.run_id,
            "createdAt": now,
            "reached": [],
            "cleaned": False,
        }
    data["mode"] = args.mode
    data["db"] = args.db
    data["volume"] = args.volume or ""
    if args.profile:
        data["profile"] = args.profile
    if args.run_dir:
        data["runDir"] = args.run_dir
    data["phase"] = phase
    if phase in PHASE_RANK and phase not in data["reached"]:
        data["reached"].append(phase)
        data["reached"].sort(key=_rank_of)
    if phase.startswith("failed:"):
        data["failed"] = phase.split(":", 1)[1]
    if phase == "cleaned":
        data["cleaned"] = True
    data["updatedAt"] = now
    state.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(state, data)
    print(f"state: run={args.run_id} phase={phase}")
    return 0


def _read_field(data: dict[str, Any], field: str) -> str:
    if field == "reached":
        return " ".join(data.get("reached", []))
    value = data.get(field, "")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def cmd_state_read(args: argparse.Namespace) -> int:
    state = Path(args.state)
    try:
        data = _load_state(state)
    except ValueError as exc:
        return _fail(f"{state}: {exc}")
    if data is None:
        return _fail(f"state-файл отсутствует: {state}")
    print(_read_field(data, args.field))
    return 0


def cmd_state_check(args: argparse.Namespace) -> int:
    """Подтверждение ownership: цели THIS прогона и нужная фаза реально достигнута."""
    state = Path(args.state)
    try:
        data = _load_state(state)
    except ValueError as exc:
        return _fail(f"{state}: {exc}")
    if data is None:
        return _fail("state-файл отсутствует — подтвердить ownership цели нечем")
    if data.get("runId") != args.run_id:
        return _fail("runId в state не совпадает с текущим прогоном")
    if args.db is not None and data.get("db", "") != args.db:
        return _fail("имя БД в state не совпадает с проверяемой целью")
    if args.volume is not None and data.get("volume", "") != args.volume:
        return _fail("имя volume в state не совпадает с проверяемой целью")
    if args.need_phase:
        reached = data.get("reached", [])
        if args.need_phase not in reached:
            return _fail(f"фаза {args.need_phase!r} не достигнута (reached={reached})")
    print("state-check: OK")
    return 0


# --------------------------------------------------------------- CLI


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="restore_targets.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate-db")
    v.add_argument("--db", required=True)
    v.add_argument("--source", required=True)
    v.add_argument("--configured", default="")
    v.set_defaults(func=cmd_validate_db)

    a = sub.add_parser("assert-absent")
    a.add_argument("--exists", required=True)
    a.set_defaults(func=cmd_assert_absent)

    t = sub.add_parser("check-tar")
    t.add_argument("--archive", required=True)
    t.add_argument("--max-bytes", required=True, type=int)
    t.set_defaults(func=cmd_check_tar)

    r = sub.add_parser("run-id")
    r.set_defaults(func=cmd_run_id)

    w = sub.add_parser("state-write")
    w.add_argument("--state", required=True)
    w.add_argument("--run-id", required=True)
    w.add_argument("--db", required=True)
    w.add_argument("--volume", default="")
    w.add_argument("--phase", required=True)
    w.add_argument("--mode", choices=("rehearsal", "cutover"), default="rehearsal")
    w.add_argument("--profile", default="")
    w.add_argument("--run-dir", default="")
    w.set_defaults(func=cmd_state_write)

    rd = sub.add_parser("state-read")
    rd.add_argument("--state", required=True)
    rd.add_argument("--field", required=True)
    rd.set_defaults(func=cmd_state_read)

    ck = sub.add_parser("state-check")
    ck.add_argument("--state", required=True)
    ck.add_argument("--run-id", required=True)
    ck.add_argument("--db")
    ck.add_argument("--volume")
    ck.add_argument("--need-phase")
    ck.set_defaults(func=cmd_state_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
