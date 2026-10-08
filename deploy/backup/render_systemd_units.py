"""Render backup systemd units for an existing deployment path.

This tool only writes a reviewable staging directory. Installing/enabling the
units remains a separate operator action after checking BACKUP_DIR and the
independent copy destination.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


UNIT_NAMES = (
    "dsbot-backup.service",
    "dsbot-backup.timer",
    "dsbot-backup-status.service",
    "dsbot-backup-status.timer",
)
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")


def render_template(template: str, deploy_dir: str) -> str:
    if not SAFE_PATH.fullmatch(deploy_dir) or "/../" in f"{deploy_dir}/":
        raise ValueError("deploy directory contains unsupported systemd path characters")
    rendered = template.replace("@DEPLOY_DIR@", deploy_dir)
    if "@DEPLOY_DIR@" in rendered:
        raise ValueError("unrendered deploy path placeholder")
    return rendered


def render(deploy_dir: Path, output_dir: Path) -> list[Path]:
    if not deploy_dir.is_absolute() or not output_dir.is_absolute():
        raise ValueError("deploy and output directories must be absolute")
    deploy = deploy_dir.resolve(strict=True)
    if not deploy.is_dir():
        raise ValueError("deploy directory does not exist")
    for script in ("backup.sh", "backup_status.sh"):
        if not (deploy / "backup" / script).is_file():
            raise ValueError(f"missing backup script: {script}")
    templates = Path(__file__).resolve().parent / "systemd"
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in UNIT_NAMES:
        template = (templates / name).read_text(encoding="utf-8")
        rendered = render_template(template, str(deploy))
        target = output_dir / name
        target.write_text(rendered, encoding="utf-8")
        written.append(target)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    for path in render(args.deploy_dir, args.output_dir):
        print(path)


if __name__ == "__main__":
    main()
