#!/usr/bin/env python3
"""T14 retention (п.8): GFS-ротация и возраст последнего проверенного recovery point.

Только stdlib — исполняется на хосте без checkout репозитория приложений
(deploy/ переносится на хост целиком). Recovery point = каталог бэкапа,
содержащий manifest.json и sidecar .verified_ok (создаётся последним шагом
finalize в backup.sh). Каталог без sidecar = незавершённый/упавший запуск:
он НЕ является точкой восстановления, НЕ считается при ротации и НЕ удаляется
автоматически (форензика; виден в status как orphaned).

Инварианты (п.4/п.8):
- удаляются только проверенные точки, попадающие в ротационный «хвост»;
- если удаление оставило бы 0 проверенных точек — удалять нечего;
- последняя завершённая проверка всегда защищена, даже старше лимитов;
- «место кончилось / планировщик молчит» видно оператору: status() возвращает
  ok=False с причиной, CLI выходит ненулевым кодом (B06).
- R26-09 (добор): `prune --execute` вне прогона больше не блокируется только
  словами runbook — скрипт сам берёт ops-lock профиля (тот же файл, что
  `acquire_ops_lock` в _backup_common.sh) до любых удалений; при занятом локе
  — ненулевой выход без единого удаления. `status`/`list`/dry-run prune
  остаются read-only и lock не берут (`backup_status.sh` не должен падать).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Host python may be 3.10 (Ubuntu 22.04) where datetime.UTC (3.11+) is absent.
UTC = timezone.utc
from pathlib import Path

# Guarded: Windows- python (на нём гоняются тесты репозитория) не имеет fcntl —
# модуль обязан оставаться импортируемым; fail-closed на отсутствие — в prune.
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

MANIFEST_NAME = "manifest.json"
VERIFIED_SIDECAR = ".verified_ok"
ORPHAN_GRACE = timedelta(days=1)  # каталог мог писаться прямо сейчас


@dataclass(frozen=True)
class Entry:
    path: str
    run_id: str
    profile: str
    created_at: datetime
    verified: bool

    @property
    def dir(self) -> Path:
        return Path(self.path)


def _parse_created(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def scan(dest: Path, profile: str | None = None) -> list[Entry]:
    """Каталоги бэкапов в dest; повреждённые/не наши каталоги отбрасываются."""
    out: list[Entry] = []
    if not dest.is_dir():
        return out
    for child in sorted(dest.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if profile and not child.name.startswith(f"dsbot-{profile}-"):
            continue
        manifest = child / MANIFEST_NAME
        verified = (child / VERIFIED_SIDECAR).exists()
        created: datetime | None = None
        run_id, prof = child.name, (profile or "")
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                created = _parse_created(data.get("createdAtUtc"))
                run_id = str(data.get("runId", run_id))
                prof = str(data.get("profile", prof))
            except (OSError, ValueError):
                created = None
        else:
            # manifest — часть finalize; без него пытаемся взять время из имени
            # dsbot-<profile>-<YYYYmmddTHHMMSSZ>[.incomplete]; не разобрали —
            # fallback на mtime каталога: упавший запуск обязан оставаться
            # виден оператору как orphan (B06-форензика), а не исчезать.
            try:
                ts = child.name.rsplit("-", 1)[-1].removesuffix(".incomplete")
                created = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            except ValueError:
                try:
                    created = datetime.fromtimestamp(child.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
        if created is None:
            # битый/неполный manifest — точка повреждена, но обязана остаться
            # видимой оператору (имя каталога → mtime).
            try:
                ts = child.name.rsplit("-", 1)[-1].removesuffix(".incomplete")
                created = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            except ValueError:
                try:
                    created = datetime.fromtimestamp(child.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
        out.append(Entry(str(child), run_id, prof, created, verified))
    return out


def plan(
    entries: list[Entry],
    now: datetime,
    daily_keep: int = 7,
    weekly_keep: int = 4,
) -> tuple[list[Entry], list[Entry]]:
    """GFS: самая свежая точка + по одной за каждый из последних daily_keep дней
    (календарных, в UTC) + по одной за неделю (ISO) в пределах weekly_keep недель.
    Возвращает (keep, delete); delete содержит только verified-точки."""
    verified = sorted((e for e in entries if e.verified), key=lambda e: e.created_at, reverse=True)
    if not verified:
        return list(entries), []
    keep: set[str] = {verified[0].path}  # последняя всегда защищена (п.8)
    if daily_keep > 0:
        days = []
        for e in verified:
            day = e.created_at.date()
            if day not in days:
                days.append(day)
        days = days[:daily_keep]
        keep.update(e.path for e in verified if e.created_at.date() in days)
    if weekly_keep > 0:
        week_newest: dict[tuple[int, int], str] = {}
        for e in verified:  # уже отсортированы newest-first
            iso = e.created_at.isocalendar()
            week_newest.setdefault((iso.year, iso.week), e.path)
        for i, path in enumerate(week_newest.values()):
            if i < weekly_keep:
                keep.add(path)
    kept = [e for e in entries if e.path in keep or not e.verified]
    deleted = [e for e in entries if e.verified and e.path not in keep]
    return kept, deleted


def status(
    entries: list[Entry],
    now: datetime,
    max_age: timedelta,
) -> tuple[bool, str]:
    """B06: возраст последней ПРОВЕРЕННОЙ точки против лимита."""
    verified = [e for e in entries if e.verified]
    if not verified:
        return False, "no verified recovery point"
    newest = max(verified, key=lambda e: e.created_at)
    age = now - newest.created_at
    orphans = [e for e in entries if not e.verified and now - e.created_at > ORPHAN_GRACE]
    detail = f"newest verified age {age.total_seconds() / 3600:.1f}h (limit {max_age.total_seconds() / 3600:.0f}h)"
    if orphans:
        detail += f"; orphaned unfinished runs: {len(orphans)}"
    ok = age <= max_age
    return ok, ("backup age ok — " if ok else "backup STALE — ") + detail


# --- R26-09 (добор): механический ops-lock для prune --execute ---
# Путь lock-файла обязан совпадать с acquire_ops_lock в _backup_common.sh:
# bash берёт flock на "$BACKUP_DIR/.ops-$PROFILE.lock", а prune вызывается с
# --dest "$BACKUP_DIR/$PROFILE", то есть dest.parent / ".ops-" + dest.name +
# ".lock". Изменение имени/расположения — строго в обоих местах сразу.
def ops_lock_path(dest: Path) -> Path:
    return dest.parent / f".ops-{dest.name}.lock"


_OPS_LOCK_FD: int | None = None  # держим открытым до выхода процесса


def _inherited_ops_lock(lock: Path) -> bool:
    """Держим ли ops-lock унаследованным fd — с ФАКТИЧЕСКИМ подтверждением блокировки.

    flock привязан к open-file-description: backup.sh держит lock на fd 9
    (`exec 9>…`), обёртка `flock -x -n FILE cmd` из runbook — на своём fd
    (flock(1) exec'ит команду с открытым fd). Новый open() того же файла —
    ДРУГОЕ описание, его собственный flock упёрся бы в EWOULDBLOCK с НАМИ ЖЕ.
    Поэтому сначала перечитываем /proc/self/fd/* (на хостах бэкапа Linux/WSL
    /proc есть; нет /proc — обычный OSError, уходим на собственный захват).

    R26-09 (PR #74 follow-up): совпадение пути у fd — только КАНДИДАТ, а не
    доказательство владения (унаследованный, но НЕ заблокированный fd раньше
    молча считался владельцем и давал ложный rc=0). Владение подтверждаем
    настоящим неблокирующим LOCK_EX на самом fd. Повторный flock на том же
    OFD, который уже заблокирован нами, — noop-успех без self-deadlock
    (сценарии backup.sh/fd9 и flock(1)); унаследованный незаблокированный fd
    этот вызов блокирует по-настоящему. OSError (EWOULDBLOCK — описание занято
    другим процессом) — fail-closed: закрываем такой fd и пробуем следующего
    кандидата; ни один не подошёл → False, и acquire_ops_lock пойдёт своим
    обычным путём (собственный open+lock), как и раньше.
    """
    global _OPS_LOCK_FD
    try:
        want = os.path.realpath(str(lock))
    except OSError:
        return False
    fd_dir = "/proc/self/fd"
    try:
        numbers = sorted(set(os.listdir(fd_dir)) | {"9"})  # (b): fd 9 — OPS_LOCK_FD из _backup_common.sh
    except OSError:
        numbers = ["9"]
    for n in numbers:
        if not n.isdigit():
            continue  # os.listdir отдаёт строки; номера fd — только цифры
        fd = int(n)
        try:
            if os.path.realpath(os.readlink(f"{fd_dir}/{n}")) != want:
                continue
        except OSError:
            continue  # fd закрыт/принадлежит не нам — не помеха
        try:
            # candidate → реальная блокировка на этом же fd (см. docstring)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            continue
        # владение доказано; fd НЕ закрываем до выхода процесса (закрытие
        # сняло бы flock) — унаследованный fd держится ровно так же, как
        # собственный fd в acquire_ops_lock
        _OPS_LOCK_FD = fd
        return True
    return False


def acquire_ops_lock(dest: Path) -> int:
    """0 — блокировка обеспечена (унаследована или захвачена), !=0 — отказ.

    Вызывается ДО любых удалений; ни одного удаления при отказе не происходит.
    """
    global _OPS_LOCK_FD
    lock = ops_lock_path(dest)
    if fcntl is None:
        # fail-closed: на не-POSIX python параллельные прогоны не исключить —
        # хосты бэкапа (Linux/WSL) fcntl имеют; импорт модуля при этом жив.
        print(f"lock: prune --execute требует POSIX-блокировки (fcntl недоступен: {lock})",
              file=sys.stderr)
        return 1
    if _inherited_ops_lock(lock):
        return 0  # унаследованный fd подтверждён реальным LOCK_EX (backup.sh/обёртка flock/свежий fd)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        print(f"lock: не удалось открыть lock-файл {lock}: {exc}", file=sys.stderr)
        return 1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        # тот же смысл отказа, что у die в bash-acquire_ops_lock
        print(f"lock: другой backup/restore выполняется (lock: {lock}) — "
              f"параллельные прогоны запрещены (R26-09)", file=sys.stderr)
        return 1
    _OPS_LOCK_FD = fd  # закрытие fd сняло бы блокировку — не закрываем до выхода
    return 0


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="backup_retention.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prune", "status", "list"):
        p = sub.add_parser(name)
        p.add_argument("--dest", required=True)
        p.add_argument("--profile")
    sub.choices["prune"].add_argument("--daily-keep", type=int, default=7)
    sub.choices["prune"].add_argument("--weekly-keep", type=int, default=4)
    sub.choices["prune"].add_argument("--execute", action="store_true",
                                      help="без флага — только план (dry-run по умолчанию, как deploy.sh)")
    sub.choices["status"].add_argument("--max-age-hours", type=float, default=26.0)
    args = ap.parse_args(argv)
    dest = Path(args.dest)
    entries = scan(dest, args.profile)
    now = datetime.now(UTC)
    if args.cmd == "list":
        for e in entries:
            print(f"{'ok ' if e.verified else '?? '} {e.created_at.isoformat()}  {e.path}")
        return 0
    if args.cmd == "status":
        ok, detail = status(entries, now, timedelta(hours=args.max_age_hours))
        print(f"backup_status: {detail}")
        return 0 if ok else 1
    if args.execute:
        # R26-09 (добор): mechanical lock BEFORE any deletion — ручной prune вне
        # прогона больше не полагается на словесную инструкцию runbook.
        rc = acquire_ops_lock(dest)
        if rc != 0:
            return rc
    kept, doomed = plan(entries, now, args.daily_keep, args.weekly_keep)
    print(f"retention: keep={len(kept)} delete={len(doomed)} (verified-only, последняя защищена)")
    for e in doomed:
        print(f"  delete {e.path}")
        if args.execute:
            import shutil

            shutil.rmtree(e.dir)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
