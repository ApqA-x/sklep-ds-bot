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
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Host python may be 3.10 (Ubuntu 22.04) where datetime.UTC (3.11+) is absent.
UTC = timezone.utc
from pathlib import Path

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
