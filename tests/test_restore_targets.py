"""R26-08 unit: deploy/backup/restore_targets.py — гейты безопасного restore.

Импортируем stdlib-хелпер напрямую (тот же приём, что в test_backup_tools).
Реального docker/Mongo здесь нет: проверяются только чистые функции-гейты.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY / "backup"))

import restore_targets  # noqa: E402

GOOD_DB = "voice_tracker_production_rehearsal_20260927T121510Z_ab12cd"
RUNID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{6}$")


def _ns(**kw) -> Namespace:
    return Namespace(**kw)


# --------------------------------------------------------------- validate-db


@pytest.mark.parametrize("name", [
    GOOD_DB,
    "voice_tracker_staging_rehearsal_20260101T000000Z_000000",
    "voice_tracker_staging_rehearsal_99999999T235959Z_ffffff",
])
def test_validate_db_accepts_allowlisted_rehearsal_names(name: str) -> None:
    rc = restore_targets.cmd_validate_db(_ns(db=name, source="voice_tracker_production", configured="voice_tracker_production"))
    assert rc == 0


@pytest.mark.parametrize("bad", [
    # усечение до часа (дефект старого ${TS:2:8})
    "voice_tracker_production_rehearsal_26092712",
    "voice_tracker_production_rehearsal_20260927T12Z_ab12cd",
    # чужой префикс /prod-имя/служебные БД
    "vs_tracker_production_rehearsal_20260927T121510Z_ab12cd",
    "voice_tracker_production",
    "voice_tracker",
    "admin", "local", "config", "app", "source",
    # инъекция в mongosh --eval
    "a';dropDatabase()",
    "x');db.getSiblingDB('admin').dropUser('u');//",
    "voice_tracker_production_rehearsal_20260927T121510Z_ab12cd';dropDatabase()",
    # регистр hex и лишние символы вне allowlist
    "voice_tracker_production_rehearsal_20260927T121510Z_AB12CD",
    "voice_tracker_production_rehearsal_20260927t121510z_ab12cd",
    "voice_tracker_production_rehearsal_20260927T121510Z_ab12cd7",
])
def test_validate_db_rejects_everything_else(bad: str, capsys) -> None:
    rc = restore_targets.cmd_validate_db(_ns(db=bad, source="voice_tracker_production", configured="voice_tracker_production"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "restore_targets:" in err  # отказ объяснён, секретов в сообщении нет


def test_validate_db_rejects_source_and_configured_even_if_shape_is_valid() -> None:
    # форма правильная, но имя совпадает с source/known configured — живой destination
    rc = restore_targets.cmd_validate_db(_ns(db=GOOD_DB, source=GOOD_DB, configured=""))
    assert rc == 2
    rc = restore_targets.cmd_validate_db(_ns(db=GOOD_DB, source="voice_tracker_production",
                                             configured=f"other,{GOOD_DB}"))
    assert rc == 2


# --------------------------------------------------------------- assert-absent


@pytest.mark.parametrize("exists", ["true", "True", "1", "yes"])
def test_assert_absent_refuses_existing_target(exists: str) -> None:
    assert restore_targets.cmd_assert_absent(_ns(exists=exists)) == 2


@pytest.mark.parametrize("exists", ["false", "False", "0", "no"])
def test_assert_absent_allows_missing_target(exists: str) -> None:
    assert restore_targets.cmd_assert_absent(_ns(exists=exists)) == 0


def test_assert_absent_rejects_garbage() -> None:
    assert restore_targets.cmd_assert_absent(_ns(exists="maybe")) == 2


# --------------------------------------------------------------- check-tar


def _tar(tmp_path: Path, name: str, members: list[tuple[str, int, str]]) -> Path:
    """members: (имя, размер, тип) — type: f|d|s|l|x"""
    p = tmp_path / name
    with tarfile.open(p, "w") as tf:
        for mname, size, mtype in members:
            info = tarfile.TarInfo(mname)
            if mtype == "f":
                info.type = tarfile.REGTYPE
                info.size = size
                tf.addfile(info, io.BytesIO(b"z" * size))
            elif mtype == "d":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif mtype == "s":
                info.type = tarfile.SYMTYPE
                info.linkname = mname
                tf.addfile(info)
            elif mtype == "l":
                info.type = tarfile.LNKTYPE
                info.linkname = "target"
                tf.addfile(info)
            else:
                info.type = tarfile.FIFOTYPE
                tf.addfile(info)
    return p


def test_check_tar_accepts_clean_archive(tmp_path: Path) -> None:
    t = _tar(tmp_path, "clean.tar", [("77", 0, "d"), ("77/2026-09/abc.bin", 100, "f"),
                                     ("./", 0, "d"), ("77/x.bin", 50, "f")])
    assert restore_targets.cmd_check_tar(_ns(archive=str(t), max_bytes=1000)) == 0


def test_check_tar_rejects_dotdot_traversal(tmp_path: Path) -> None:
    t = _tar(tmp_path, "trav.tar", [("../evil.sh", 10, "f")])
    assert restore_targets.cmd_check_tar(_ns(archive=str(t), max_bytes=1000)) == 2


def test_check_tar_rejects_inner_dotdot_component(tmp_path: Path) -> None:
    t = _tar(tmp_path, "trav2.tar", [("a/../../etc/passwd", 10, "f")])
    assert restore_targets.cmd_check_tar(_ns(archive=str(t), max_bytes=1000)) == 2


def test_check_tar_rejects_absolute_path(tmp_path: Path) -> None:
    t = _tar(tmp_path, "abs.tar", [("/etc/shadow", 10, "f")])
    assert restore_targets.cmd_check_tar(_ns(archive=str(t), max_bytes=1000)) == 2


def test_check_tar_rejects_symlink_hardlink_fifo(tmp_path: Path) -> None:
    for mtype in ("s", "l", "x"):
        t = _tar(tmp_path, f"link-{mtype}.tar", [("ok.bin", 1, "f"), (f"bad-{mtype}", 0, mtype)])
        assert restore_targets.cmd_check_tar(_ns(archive=str(t), max_bytes=1000)) == 2


def test_check_tar_rejects_total_size_over_ceiling(tmp_path: Path) -> None:
    t = _tar(tmp_path, "big.tar", [("a.bin", 600, "f"), ("b.bin", 600, "f")])
    assert restore_targets.cmd_check_tar(_ns(archive=str(t), max_bytes=1000)) == 2
    assert restore_targets.cmd_check_tar(_ns(archive=str(t), max_bytes=1200)) == 0


def test_check_tar_rejects_unreadable_and_missing(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar"
    junk.write_bytes(b"not a tar at all")
    assert restore_targets.cmd_check_tar(_ns(archive=str(junk), max_bytes=1000)) == 2
    assert restore_targets.cmd_check_tar(_ns(archive=str(tmp_path / "nope.tar"), max_bytes=1000)) == 2


# --------------------------------------------------------------- run-id


def test_run_id_full_precision_and_unique() -> None:
    a = restore_targets.new_run_id()
    b = restore_targets.new_run_id()
    assert RUNID_RE.match(a), a
    assert RUNID_RE.match(b), b
    assert a != b  # энтропия secrets: два вызова в одну секунду не коллидируют
    # DB-имя, собранное из run id, проходит allowlist
    assert restore_targets.DB_ALLOWLIST_RE.match(f"voice_tracker_production_rehearsal_{a}")


# --------------------------------------------------------------- state


def _sw(tmp_path: Path, phase: str, run_id: str = "20260927T121510Z_ab12cd",
        db: str = GOOD_DB, volume: str = "dsbot-production-restore-media-x",
        state: str | None = None) -> int:
    return restore_targets.cmd_state_write(_ns(
        state=str(state or (tmp_path / "state.json")), run_id=run_id, db=db,
        volume=volume, phase=phase, mode="rehearsal", profile="production",
        run_dir="/b/dsbot-production-1"))


def test_state_write_roundtrip_and_phase_progression(tmp_path: Path) -> None:
    assert _sw(tmp_path, "prepared") == 0
    assert _sw(tmp_path, "media-extracted") == 0
    assert _sw(tmp_path, "db-restored") == 0
    assert _sw(tmp_path, "verified") == 0
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert data["runId"] == "20260927T121510Z_ab12cd"
    assert data["db"] == GOOD_DB
    assert data["phase"] == "verified"
    assert data["reached"] == ["prepared", "media-extracted", "db-restored", "verified"]
    assert data["mode"] == "rehearsal"
    assert data["runDir"] == "/b/dsbot-production-1"
    assert not data["cleaned"]


def test_state_write_rejects_second_run_on_same_state_file(tmp_path: Path) -> None:
    # «повтор в один час»: новый run id в тот же state — отказ, файл не тронут
    assert _sw(tmp_path, "prepared") == 0
    before = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert _sw(tmp_path, "prepared", run_id="20260927T121511Z_000000") == 2
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == before


def test_state_write_rejects_target_drift_and_phase_regression(tmp_path: Path) -> None:
    assert _sw(tmp_path, "media-extracted") == 0
    assert _sw(tmp_path, "verified", db="voice_tracker_staging_rehearsal_20260101T000000Z_000000") == 2
    assert _sw(tmp_path, "verified", volume="someone-elses-volume") == 2
    # регрессия ранга — отказ (media-extracted=2 уже достигнуто, prepared=1 нет)
    assert _sw(tmp_path, "prepared") == 2
    # равный ранг — идемпотентно (resume того же шага)
    assert _sw(tmp_path, "media-extracted") == 0


def test_state_write_failed_and_cleaned_semantics(tmp_path: Path) -> None:
    assert _sw(tmp_path, "prepared") == 0
    assert _sw(tmp_path, "media-extracted") == 0
    assert _sw(tmp_path, "failed:media") == 0
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert data["phase"] == "failed:media" and data["failed"] == "media"
    assert data["reached"] == ["prepared", "media-extracted"]  # диагностика не стёрта
    assert _sw(tmp_path, "cleaned") == 0
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert data["cleaned"] is True
    # после cleaned обычные фазы уже не пишутся
    assert _sw(tmp_path, "prepared") == 2


def test_state_write_rejects_unknown_phase_and_corrupt_file(tmp_path: Path) -> None:
    assert _sw(tmp_path, "exploded") == 2
    bad = tmp_path / "state.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _sw(tmp_path, "prepared") == 2
    assert bad.read_text(encoding="utf-8") == "{not json"  # повреждённый не затирается


def test_state_read_and_check_gate_db_drop_ownership(tmp_path: Path) -> None:
    state = str(tmp_path / "state.json")
    assert _sw(tmp_path, "prepared", state=state) == 0
    # БД ещё не восстанавливалась — право на dropDatabase ОТКАЗАНО
    assert restore_targets.cmd_state_check(_ns(
        state=state, run_id="20260927T121510Z_ab12cd", db=GOOD_DB, volume=None,
        need_phase="db-restored")) == 2
    assert _sw(tmp_path, "db-restored", state=state) == 0
    assert restore_targets.cmd_state_check(_ns(
        state=state, run_id="20260927T121510Z_ab12cd", db=GOOD_DB, volume=None,
        need_phase="db-restored")) == 0
    # чужой run id / чужое имя БД — отказ, даже если фаза достигнута
    assert restore_targets.cmd_state_check(_ns(
        state=state, run_id="other", db=GOOD_DB, volume=None, need_phase=None)) == 2
    assert restore_targets.cmd_state_check(_ns(
        state=state, run_id="20260927T121510Z_ab12cd",
        db="voice_tracker_production", volume=None, need_phase="db-restored")) == 2
    assert restore_targets.cmd_state_read(_ns(state=state, field="runId")) == 0
    assert restore_targets.cmd_state_read(_ns(state=str(tmp_path / "nope.json"), field="runId")) == 2


def test_state_write_atomic_file_mode(tmp_path: Path) -> None:
    assert _sw(tmp_path, "prepared") == 0
    p = tmp_path / "state.json"
    assert p.exists() and not list(tmp_path.glob(".state-*"))  # временный файл переименован
    assert p.read_text(encoding="utf-8").startswith("{")  # целостный JSON, не осколок записи
    if os.name != "nt":  # на NTFS/drvfs POSIX-биты не выразимы
        assert (p.stat().st_mode & 0o777) == 0o600
