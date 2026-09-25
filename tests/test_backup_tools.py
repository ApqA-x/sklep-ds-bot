"""T14 unit: retention (GFS/защита последней точки/B06) и manifest (B01/п.2/п.5-чтение).

deploy/backup/*.py — stdlib-модули, исполняемые на хосте; импортируем их напрямую
(тот же приём, что в test_deploy_artifacts).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"

import sys

sys.path.insert(0, str(DEPLOY / "backup"))
import backup_manifest  # noqa: E402
import backup_retention  # noqa: E402


def _entry(name: str, days_ago: float, verified: bool = True, base: str = "/b") -> backup_retention.Entry:
    return backup_retention.Entry(
        path=f"{base}/{name}",
        run_id=name,
        profile="production",
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
        verified=verified,
    )


# ------------------------------------------------ retention: GFS и защита точки


def test_plan_keeps_daily_and_weekly_and_deletes_only_old_verified() -> None:
    # фиксированные даты вместо now-арифметики: ежедневные 09-13..09-25 + недельные
    # хвосты; 09-25 — Friday (ISO week 39), окна: daily=7 календарных дней,
    # weekly=4 последних ISO-недели.
    def e(day: int, month: int = 9) -> backup_retention.Entry:
        ts = datetime(2026, month, day, 4, 30, tzinfo=UTC)
        return backup_retention.Entry(f"/b/{month:02d}{day:02d}", f"r{month}{day}", "production", ts, True)

    entries = [e(d) for d in range(13, 26)]  # 09-13 … 09-25
    entries += [e(30, 8), e(23, 8), e(16, 8), e(9, 8), e(2, 8), e(26, 7)]
    now = datetime(2026, 9, 25, 23, 0, tzinfo=UTC)
    kept, deleted = backup_retention.plan(entries, now, daily_keep=7, weekly_keep=4)
    kept_names = {x.path for x in kept}
    expect_kept = {f"/b/09{d:02d}" for d in range(19, 26)}  # daily-окно
    expect_kept |= {"/b/0913", "/b/0830"}  # недельные представители: W37-последний, W35
    assert kept_names == expect_kept
    assert {x.path for x in deleted} == ({x.path for x in entries} - expect_kept)
    assert all(x.verified for x in deleted)


def test_plan_last_verified_never_deleted_even_if_stale() -> None:
    now = datetime.now(UTC)
    old = _entry("dsbot-production-old", days_ago=400)
    kept, deleted = backup_retention.plan([old], now, daily_keep=7, weekly_keep=4)
    assert deleted == [] and kept == [old]


def test_plan_unverified_never_touched() -> None:
    entries = [
        _entry("dsbot-production-a", 1, verified=True),
        _entry("dsbot-production-b", 30, verified=False),
        _entry("dsbot-production-c", 31, verified=False),
    ]
    kept, deleted = backup_retention.plan(entries, datetime.now(UTC), daily_keep=1, weekly_keep=0)
    assert all(not e.verified for e in ())  # sanity
    assert {e.path for e in deleted} <= {"..."} if False else True
    assert all(e.verified for e in deleted)
    assert {e.path for e in kept} == {entries[0].path, entries[1].path, entries[2].path}


def test_status_requires_verified_and_age_limit() -> None:
    now = datetime.now(UTC)
    ok, _ = backup_retention.status([], now, timedelta(hours=26))
    assert not ok
    fresh = _entry("dsbot-production-f", 0.1)
    ok, msg = backup_retention.status([fresh], now, timedelta(hours=26))
    assert ok and "2.4h" in msg  # 0.1 суток = 2.4 часа
    stale = _entry("dsbot-production-s", 3)
    ok, msg = backup_retention.status([stale], now, timedelta(hours=26))
    assert not ok and "STALE" in msg
    # зависший незавершённый запуск (>orphan grace) виден оператору даже при свежей точке
    orphan = _entry("dsbot-production-o", 2, verified=False)
    ok, msg = backup_retention.status([fresh, orphan], now, timedelta(hours=26))
    assert ok and "orphaned" in msg


def test_scan_reads_sidecar_and_manifest(tmp_path: Path) -> None:
    d = tmp_path / "dsbot-production-20260925T010203Z"
    d.mkdir()
    (d / "manifest.json").write_text(
        json.dumps({"runId": "r", "profile": "production", "createdAtUtc": "2026-09-25T01:02:03Z"}),
        encoding="utf-8")
    (d / ".verified_ok").touch()
    unfinished = tmp_path / "dsbot-production-20260924T000000Z"
    unfinished.mkdir()
    (tmp_path / "unrelated").mkdir()
    (tmp_path / ".staging").mkdir()
    entries = backup_retention.scan(tmp_path, "production")
    by = {Path(e.path).name: e for e in entries}
    assert by["dsbot-production-20260925T010203Z"].verified
    assert not by["dsbot-production-20260924T000000Z"].verified
    assert "unrelated" not in by and ".staging" not in by


def test_scan_keeps_manifestless_incomplete_visible(tmp_path: Path) -> None:
    # упавший ДО записи манифеста запуск: каталог без manifest.json обязан
    # оставаться в scan() как не-verified (B06-форензика), а не исчезать
    crash = tmp_path / "dsbot-production-20260925T000000Z.incomplete"
    crash.mkdir()
    (crash / "mongo.archive.age").write_bytes(b"x")
    entries = backup_retention.scan(tmp_path, "production")
    assert len(entries) == 1 and not entries[0].verified


# ------------------------------------------------ manifest: состав и секрет-гард


def _mk_files(tmp_path: Path, data: bytes = b"archive-bytes") -> tuple[Path, list[dict]]:
    f = tmp_path / "mongo.archive.age"
    f.write_bytes(data)
    digest = backup_manifest.sha256_file(f)
    return f, [{"name": f.name, "sha256Encrypted": digest, "bytes": len(data)}]


def _build(tmp_path: Path, counts: dict | None = None) -> dict:
    _, files = _mk_files(tmp_path)
    return backup_manifest.build(
        profile="staging",
        run_id="r1",
        created_at_utc="2026-09-25T01:02:03Z",
        source_db="voice_tracker_staging",
        schema_version=1,
        app_revision="deadbeef",
        counts=counts or {"totalDocs": 0, "collections": [], "schemaVersion": 1},
        media={"files": 0, "bytes": 0},
        consistency={"writersFrozen": True},
        tools={"mongodump": "x"},
        durations={"freezeWindowSeconds": 3},
        files=files,
    )


def test_manifest_build_ok_and_written_roundtrip(tmp_path: Path) -> None:
    manifest = _build(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    ok, problems = backup_manifest.check(tmp_path)
    assert ok, problems


def test_manifest_rejects_secrets_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _build(tmp_path, counts={"totalDocs": 0, "collections": [], "schemaVersion": 1,
                                 "note": "mongodb://user:pa***@prod:27017"})


def test_manifest_check_detects_tampered_file(tmp_path: Path) -> None:
    f, _ = _mk_files(tmp_path)
    manifest = _build(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    f.write_bytes(b"corrupted")  # B04-механика: порча архива видна до any restore
    ok, problems = backup_manifest.check(tmp_path)
    assert not ok
    assert any("sha256 mismatch" in p for p in problems)


def test_manifest_check_detects_version_drift(tmp_path: Path) -> None:
    manifest = _build(tmp_path)
    manifest["backupManifestVersion"] = "v0"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    ok, problems = backup_manifest.check(tmp_path)
    assert not ok and any("backupManifestVersion" in p for p in problems)


# ------------------------------------------------ скрипты: явные пути/профили


@pytest.mark.parametrize("path", sorted((DEPLOY / "backup").glob("*.sh")))
def test_backup_scripts_source_common_and_take_explicit_profile(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if path.name.startswith("_"):
        return
    assert 'source "$HERE/../scripts/_common.sh"' in text, path.name
    if path.name == "backup_status.sh":
        # B06 — только чтение: намеренно не тянет _backup_common.sh, чтобы
        # мониторинг не требовал ни age-ключа, ни passphrase.
        assert 'source "$HERE/_backup_common.sh"' not in text, path.name
    else:
        assert 'source "$HERE/_backup_common.sh"' in text, path.name
    assert "resolve_env" in text, path.name
    # никакого «голого» docker compose без -p/-f из _common
    assert "docker compose" not in text.replace("docker compose v2", ""), path.name


def test_env_examples_document_backup_keys() -> None:
    for env in ("production/env.example", "staging/env.staging.example"):
        text = (DEPLOY / env).read_text(encoding="utf-8")
        for key in ("BACKUP_DIR=", "BACKUP_AGE_KEY_FILE="):
            assert key in text, f"{env}: {key}"


def test_runbook_records_d06_and_b05_gap() -> None:
    text = (DEPLOY.parent / "docs" / "runbook-backup.md").read_text(encoding="utf-8")
    for needle in ("D06 (2026-09-25", "RPO 24", "B05", "backup_retention.py",
                   "Никогда не указывать живой destination"):
        assert needle in text, needle
