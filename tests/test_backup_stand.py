"""T14 интеграция: backup/restore round-trip против реальной Mongo стенда.

Механика B01/B03/B04 без compose-профиля (полный pipeline — live-репетиция):
синтетический набор (guild_settings с revision, chat_messages с attachments,
schema_versions, индексы из канонического манифеста) → mongodump-архив (docker
exec в контейнер стенда) → манифест+sha → check ловит подмену байта (B04) →
mongorestore в НОВУЮ БД (п.5: никогда в живую) → backup_report.verify против
манифеста: counts, индексы через схему-контракт, revision, наличие media-файлов
по attachments.path, schemaVersion (B03/п.6); негатив: удаление файла из media
ломает verify.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402
from voice_tracker import backup_report, schema  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "backup"))
import backup_manifest  # noqa: E402

pytestmark = pytest.mark.integration

TEST_MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")
STAND_MONGO_CONTAINER = os.environ.get("TEST_STAND_MONGO_CONTAINER", "dsbot-test-mongo")


def _docker() -> str:
    d = shutil.which("docker") or os.environ.get("DOCKER_CLI", "")
    if not d or subprocess.run([d, "version"], capture_output=True).returncode != 0:
        pytest.skip("docker CLI недоступна из тестового хоста")
    return d


def _server_up() -> bool:
    try:
        c = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500)
        c.admin.command("ping")
        c.close()
        return True
    except Exception:
        return False


guard_mongo_uri(TEST_MONGO_URI)
if not _server_up():
    pytest.skip(f"стендовый mongod не отвечает на {TEST_MONGO_URI}", allow_module_level=True)


def _new_test_db(name_hint: str) -> str:
    db = f"voice_tracker_t14_{name_hint}_{secrets.token_hex(4)}"
    guard_db_name(db)
    return db


def _seed(client: MongoClient, db: str, media_root: Path) -> None:
    for spec in schema.MANIFEST:
        client[db][spec.collection].create_index(list(spec.keys), **spec.create_kwargs())
    d = client[db]
    d["guild_settings"].insert_one({"_id": "g1", "guildId": "77", "revision": 3})
    d["chat_messages"].insert_one({
        "_id": "m1", "guildId": "77",
        "attachments": [
            {"id": "a1", "stored": True, "path": "77/2026-09/abc.bin"},
            {"id": "a2", "stored": False, "path": ""},
        ],
    })
    d["schema_versions"].insert_one({"schemaVersion": 1, "checksum": "x" * 64})
    f = media_root / "77" / "2026-09"
    f.mkdir(parents=True)
    (f / "abc.bin").write_bytes(b"content")


@pytest.fixture()
def mongo() -> MongoClient:
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=3000)
    yield client
    client.close()


def _exec_stream(docker: str, inner: list[str], *, stdin: bytes | None = None,
                 to_file: Path | None = None) -> bytes:
    cmd = [docker, "exec"] + (["-i"] if stdin is not None else []) + \
          [STAND_MONGO_CONTAINER, "sh", "-c", " ".join(inner)]
    proc = subprocess.run(cmd, input=stdin, capture_output=True)
    assert proc.returncode == 0, proc.stderr[:800]
    if to_file:
        to_file.write_bytes(proc.stdout)
        return b""
    return proc.stdout


def test_full_dump_restore_verify_roundtrip(tmp_path: Path, mongo: MongoClient) -> None:
    docker = _docker()
    src, dst = _new_test_db("src"), _new_test_db("dst")
    media = tmp_path / "media"
    try:
        _seed(mongo, src, media)

        counts = backup_report.counts(TEST_MONGO_URI, src)
        by_name = {c["name"]: c for c in counts["collections"]}
        assert by_name["guild_settings"]["count"] == 1
        assert counts["schemaVersion"] == 1

        run = tmp_path / f"dsbot-staging-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        run.mkdir()
        archive = run / "mongo.archive.age"
        _exec_stream(docker, [f"mongodump --quiet --db {src} --archive"], to_file=archive)
        assert archive.stat().st_size > 0

        manifest = backup_manifest.build(
            profile="staging", run_id=run.name, created_at_utc="2026-09-25T12:00:00Z",
            source_db=src, schema_version=counts["schemaVersion"], app_revision="t14test",
            counts=counts, media=backup_report.media_manifest(str(media)),
            consistency={"writersFrozen": True, "consistentSnapshot": True},
            tools={"mongodump": "test"}, durations={"freezeWindowSeconds": 1},
            files=[{"name": archive.name,
                    "sha256Encrypted": backup_manifest.sha256_file(archive),
                    "bytes": archive.stat().st_size}],
        )
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        ok, problems = backup_manifest.check(run)
        assert ok, problems  # B01: манифест собран, секрет-гард прошёл, файлы на месте

        # B04: подмена байта в архиве ловится ДО восстановления
        good = archive.read_bytes()
        archive.write_bytes(good[: len(good) // 2] + bytes([good[len(good) // 2] ^ 0xFF])
                            + good[len(good) // 2 + 1:])
        ok, problems = backup_manifest.check(run)
        assert not ok and any("sha256" in p for p in problems)
        archive.write_bytes(good)

        _exec_stream(docker, [
            f"mongorestore --quiet --drop --archive --nsInclude='{src}.*'"
            f" --nsFrom='{src}.*' --nsTo='{dst}.*'"], stdin=good)

        report = backup_report.verify(TEST_MONGO_URI, dst, run / "manifest.json", str(media))
        failed = [c["name"] for c in report["checks"] if not c["ok"]]
        assert report["ok"], failed  # B03: counts/индексы/revision/media-соответствие

        # п.6 негатив: потерян физический файл при живом metadata-референсе
        (media / "77" / "2026-09" / "abc.bin").unlink()
        report = backup_report.verify(TEST_MONGO_URI, dst, run / "manifest.json", str(media))
        assert not report["ok"]
        names = {c["name"]: c for c in report["checks"]}
        assert not names["attachments-files-exist"]["ok"]
    finally:
        for db in (src, dst):
            mongo.drop_database(db)
