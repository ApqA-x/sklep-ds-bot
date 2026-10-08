from __future__ import annotations

import os
from pathlib import Path

import pytest

from deploy.backup.render_systemd_units import UNIT_NAMES, render, render_template


def test_service_paths_are_rendered_from_real_deploy_directory() -> None:
    templates = Path(__file__).resolve().parents[1] / "deploy" / "backup" / "systemd"
    for name in UNIT_NAMES:
        rendered = render_template((templates / name).read_text(encoding="utf-8"), "/home/apqa/estera/bot/deploy")
        assert "@DEPLOY_DIR@" not in rendered
        assert "/opt/dsbot/deploy" not in rendered
        if name.endswith(".service"):
            assert "WorkingDirectory=/home/apqa/estera/bot/deploy" in rendered
            assert "ExecStart=/home/apqa/estera/bot/deploy/backup/" in rendered


@pytest.mark.parametrize("path", ["relative/deploy", "/tmp/a b", "/tmp/../etc", "/tmp/x\nExecStart=/bin/false"])
def test_unsafe_deploy_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        render_template("WorkingDirectory=@DEPLOY_DIR@", path)


@pytest.mark.skipif(os.name == "nt", reason="Linux systemd path renderer")
def test_render_requires_existing_backup_scripts_and_writes_only_stage(tmp_path: Path) -> None:
    deploy = tmp_path / "bot" / "deploy"
    backup = deploy / "backup"
    backup.mkdir(parents=True)
    for script in ("backup.sh", "backup_status.sh"):
        (backup / script).write_text("#!/bin/sh\n", encoding="utf-8")
    stage = tmp_path / "units"
    written = render(deploy, stage)
    assert {path.name for path in written} == set(UNIT_NAMES)
    assert all(path.parent == stage for path in written)
    assert not (deploy / "systemd").exists()
