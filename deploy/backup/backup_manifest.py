#!/usr/bin/env python3
"""T14 (п.2) единый versioned backup manifest + его проверка.

stdlib-only (host-side). manifest.json — «доказательство» точки восстановления:
время, источник (имя БД, app revision, schemaVersion), counts коллекций, размеры,
checksums файлов, media manifest, статус границы согласованности, версии инструментов.
Сырых документов, URI и секретов здесь нет; build() проверяет это перед записью
(B01), check() перечитывает и сверяет с фактическими файлами каталога (п.5).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

BACKUP_MANIFEST_VERSION = "dsbot-backup-manifest/v1"
MANIFEST_NAME = "manifest.json"

# в манифесте не должно быть ни URI с credentials, ни длинных hex-секретов,
# ни путей хоста с токенами; проверяем весь сериализованный JSON.
_FORBIDDEN = re.compile(
    r"(mongodb://|mysql://|nats://|amqp://|redis://"
    r"|authorization|bearer |x-access-token"
    r"|passphrase|password|secret)",
    re.IGNORECASE,
)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def build(
    *,
    profile: str,
    run_id: str,
    created_at_utc: str,
    source_db: str,
    schema_version: int | None,
    app_revision: str | None,
    counts: dict[str, Any],
    media: dict[str, Any],
    consistency: dict[str, Any],
    tools: dict[str, str],
    durations: dict[str, float],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "backupManifestVersion": BACKUP_MANIFEST_VERSION,
        "profile": profile,
        "runId": run_id,
        "createdAtUtc": created_at_utc,
        "source": {
            "db": source_db,
            "schemaVersion": schema_version,
            "appRevision": app_revision,
        },
        "collections": counts,
        "media": media,
        "consistency": consistency,
        "tools": tools,
        "durationsSeconds": durations,
        "files": files,
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    if _FORBIDDEN.search(text):
        # не пишем и не показываем содержимое — только имя запрещённой группы
        raise ValueError("manifest содержит запрещённый фрагмент (URI/credentials/secret)")
    return manifest


def assert_secret_free(manifest: dict[str, Any]) -> None:
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    if _FORBIDDEN.search(text):
        raise ValueError("manifest содержит запрещённый фрагмент (URI/credentials/secret)")


def check(run_dir: Path) -> tuple[bool, list[str]]:
    """Перечитать точку восстановления: version, наличие всех файлов, их sha256."""
    problems: list[str] = []
    mpath = run_dir / MANIFEST_NAME
    if not mpath.exists():
        return False, [f"missing {MANIFEST_NAME}"]
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, [f"manifest unreadable: {exc}"]
    if manifest.get("backupManifestVersion") != BACKUP_MANIFEST_VERSION:
        problems.append(f"unexpected backupManifestVersion {manifest.get('backupManifestVersion')!r}")
    try:
        assert_secret_free(manifest)
    except ValueError as exc:
        problems.append(str(exc))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        problems.append("manifest.files missing/empty")
        return False, problems
    for entry in files:
        name = str(entry.get("name", ""))
        want = str(entry.get("sha256Encrypted", ""))
        p = run_dir / name
        if not name or not p.is_file():
            problems.append(f"file missing: {name}")
            continue
        got = sha256_file(p)
        if got != want:
            problems.append(f"sha256 mismatch: {name}")
        size = entry.get("bytes")
        if isinstance(size, int) and p.stat().st_size != size:
            problems.append(f"size mismatch: {name}")
    return not problems, problems


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="backup_manifest.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--run-dir", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    b.add_argument("--profile", required=True)
    b.add_argument("--run-id", required=True)
    b.add_argument("--created-at", required=True)
    b.add_argument("--source-db", required=True)
    b.add_argument("--schema-version", type=int)
    b.add_argument("--app-revision")
    b.add_argument("--counts-file", required=True, help="JSON из backup_report counts")
    b.add_argument("--media-file", required=True, help="JSON media-manifest")
    b.add_argument("--consistency-file", required=True)
    b.add_argument("--tools-file", required=True)
    b.add_argument("--durations-file", required=True)
    b.add_argument("--files-file", required=True, help="JSON-список [{name,sha256Encrypted,bytes}]")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        ok, problems = check(Path(args.run_dir))
        for p in problems:
            print(f"manifest check: {p}", file=sys.stderr)
        print("manifest check: " + ("OK" if ok else "FAILED"))
        return 0 if ok else 1
    manifest = build(
        profile=args.profile,
        run_id=args.run_id,
        created_at_utc=args.created_at,
        source_db=args.source_db,
        schema_version=args.schema_version,
        app_revision=args.app_revision,
        counts=json.loads(Path(args.counts_file).read_text(encoding="utf-8")),
        media=json.loads(Path(args.media_file).read_text(encoding="utf-8")),
        consistency=json.loads(Path(args.consistency_file).read_text(encoding="utf-8")),
        tools=json.loads(Path(args.tools_file).read_text(encoding="utf-8")),
        durations=json.loads(Path(args.durations_file).read_text(encoding="utf-8")),
        files=json.loads(Path(args.files_file).read_text(encoding="utf-8")),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
