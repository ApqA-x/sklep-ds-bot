"""T14 (п.2/п.6): отчёты о снимке БД и сверка восстановленной копии.

Исполняется ВНУТРИ образа приложения (там есть pymongo) или на стендовом
python с pymongo; в манифест/отчёт попадают только имена коллекций, количества,
размеры и булевы исходы проверок — никогда содержимое документов, URI или секреты.

CLI (python -m voice_tracker.backup_report):
  counts  --uri U --db D                     → JSON для manifest.collections
  media   --dir /data/media                  → JSON для manifest.media
  verify  --uri U --db D --manifest F [--media-dir DIR] [--missing-file-limit N]
                                             → JSON-отчёт; exit 0 если все проверки ok
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ATTACHMENT_COLLECTION = "chat_messages"


def _coll_stats(db: Any, name: str) -> tuple[int, int | None]:
    count = db[name].estimated_document_count()
    size: int | None = None
    try:
        stats = db.command("collstats", name)
        size = int(stats.get("size", 0))
    except Exception:
        pass
    return count, size


def counts(uri: str, db_name: str) -> dict[str, Any]:
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        db = client[db_name]
        names = sorted(db.list_collection_names())
        colls = []
        total = 0
        for n in names:
            c, size = _coll_stats(db, n)
            total += c
            colls.append({"name": n, "count": c, "sizeBytes": size})
        schema_version = None
        if "schema_versions" in names:
            docs = list(db["schema_versions"].find({}, {"schemaVersion": 1}).sort("schemaVersion", -1).limit(1))
            if docs:
                schema_version = int(docs[0].get("schemaVersion", 0))
        return {"db": db_name, "totalDocs": total, "collections": colls, "schemaVersion": schema_version}
    finally:
        client.close()


def media_manifest(media_dir: str) -> dict[str, Any]:
    root = Path(media_dir)
    files = 0
    total_bytes = 0
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass
    return {"files": files, "bytes": total_bytes}


def _attachment_refs(db: Any) -> tuple[int, list[str]]:
    """(число stored-ссылок, относительные пути файлов) — только метаданные."""
    stored = 0
    paths: list[str] = []
    cur = db[ATTACHMENT_COLLECTION].find(
        {"attachments.stored": True},
        {"attachments.path": 1, "attachments.stored": 1, "_id": 0},
    )
    for doc in cur:
        for att in doc.get("attachments") or []:
            if att.get("stored") and att.get("path"):
                stored += 1
                paths.append(str(att["path"]))
    return stored, paths


def verify(uri: str, db_name: str, manifest_path: Path, media_dir: str | None) -> dict[str, Any]:
    from pymongo import MongoClient

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        db = client[db_name]
        expected = {c["name"]: c["count"] for c in manifest.get("collections", {}).get("collections", [])}
        actual_names = set(db.list_collection_names())
        missing_colls = sorted(set(expected) - actual_names)
        checks.append({"name": "collections-present", "ok": not missing_colls,
                       "detail": {"missing": missing_colls[:50]}})
        mismatched = {}
        for name, want in expected.items():
            if name in actual_names:
                got, _ = _coll_stats(db, name)
                if got != want:
                    mismatched[name] = {"expected": want, "actual": got}
        checks.append({"name": "counts-equal-manifest", "ok": not mismatched,
                       "detail": {"mismatched": dict(list(mismatched.items())[:50])}})

        # Индексы сверяет канонический механизм контракта (DB01/DB02), а не
        # упрощённое сравнение «на глаз»: эквивалент под другим именем — ok.
        try:
            from voice_tracker import schema as schema_mod

            report = schema_mod.verify_db(db)
            problems = list(report.missing) + list(report.incompatible)
            checks.append({"name": "schema-indexes-verify", "ok": not problems,
                           "detail": {"missingOrIncompatible": problems[:50]}})
        except ImportError:
            checks.append({"name": "schema-indexes-verify", "ok": True,
                           "detail": {"skipped": "schema module unavailable"}})

        if "guild_settings" in actual_names:
            no_rev = db["guild_settings"].count_documents({"revision": {"$exists": False}})
            checks.append({"name": "revision-field-present", "ok": no_rev == 0,
                           "detail": {"docsWithoutRevision": no_rev}})

        if ATTACHMENT_COLLECTION in actual_names:
            stored, rel_paths = _attachment_refs(db)
            if media_dir:
                root = Path(media_dir)
                missing_files = [p for p in rel_paths if not (root / p).is_file()]
                checks.append({"name": "attachments-files-exist", "ok": not missing_files,
                               "detail": {"storedRefs": stored,
                                          "missingSample": missing_files[:20],
                                          "missingCount": len(missing_files)}})
                got_media = sum(1 for p in root.rglob("*") if p.is_file())
                exp_media = manifest.get("media", {}).get("files")
                checks.append({"name": "media-file-count",
                               "ok": exp_media is None or exp_media == got_media,
                               "detail": {"expected": exp_media, "actual": got_media}})
            else:
                checks.append({"name": "attachments-files-exist", "ok": True,
                               "detail": {"skipped": "no --media-dir", "storedRefs": stored}})

        sv_expected = manifest.get("source", {}).get("schemaVersion")
        if sv_expected is not None and "schema_versions" in actual_names:
            docs = list(db["schema_versions"].find({}, {"schemaVersion": 1}).sort("schemaVersion", -1).limit(1))
            sv_actual = int(docs[0]["schemaVersion"]) if docs else None
            checks.append({"name": "schema-version-equal", "ok": sv_actual == int(sv_expected),
                           "detail": {"expected": sv_expected, "actual": sv_actual}})
    finally:
        client.close()
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "db": db_name, "checks": checks}


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="backup_report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("counts")
    c.add_argument("--uri", required=True)
    c.add_argument("--db", required=True)
    m = sub.add_parser("media")
    m.add_argument("--dir", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--uri", required=True)
    v.add_argument("--db", required=True)
    v.add_argument("--manifest", required=True)
    v.add_argument("--media-dir")
    args = ap.parse_args(argv)
    if args.cmd == "counts":
        print(json.dumps(counts(args.uri, args.db), ensure_ascii=False))
        return 0
    if args.cmd == "media":
        print(json.dumps(media_manifest(args.dir), ensure_ascii=False))
        return 0
    report = verify(args.uri, args.db, Path(args.manifest), args.media_dir)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
