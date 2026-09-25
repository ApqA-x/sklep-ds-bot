import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from voice_tracker import media as media_module
from voice_tracker.media import classify, relative_path, store_attachments


class FakeAttachment:
    def __init__(self, *, att_id, filename, content_type, size, data, url="https://cdn.discord/attach"):
        self.id = att_id
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self._data = data
        self.url = url
        self.proxy_url = url

    async def read(self, use_cached=False):
        return self._data


def test_classify():
    assert classify("image/png", "x") == "image"
    assert classify("video/webm", "clip") == "video"
    assert classify("audio/mpeg", "song.mp3") == "audio"
    assert classify("application/zip", "archive.zip") == "file"
    assert classify("", "photo.JPG") == "image"


def test_relative_path_shape():
    rel = relative_path("99", datetime(2026, 9, 22, tzinfo=timezone.utc), "abc123", ".png")
    assert rel == "99/2026-09/abc123.png"


def test_store_attachments_saves_images_only(tmp_path):
    png = FakeAttachment(att_id="1", filename="cat.png", content_type="image/png", size=4, data=b"PNG1")
    big = FakeAttachment(att_id="2", filename="huge.png", content_type="image/png", size=99 * 1024 * 1024, data=b"x")
    zipf = FakeAttachment(att_id="3", filename="a.zip", content_type="application/zip", size=2, data=b"ZZ")

    meta = asyncio.run(store_attachments(str(tmp_path), "170000000000000000", [png, big, zipf]))

    assert meta[0]["stored"] is True
    assert meta[0]["kind"] == "image"
    assert meta[0]["path"].startswith("170000000000000000/")
    saved = (tmp_path / meta[0]["path"]).read_bytes()
    assert saved == b"PNG1"
    # слишком большой файл не скачиваем вообще (вызов read() упал бы)
    assert meta[1]["stored"] is False and meta[1]["url"]
    # не-картинки — только метаданные
    assert meta[2]["stored"] is False and meta[2]["kind"] == "file"


def test_store_attachments_dedupes_and_disabled_dir(tmp_path):
    one = FakeAttachment(att_id="1", filename="a.png", content_type="image/png", size=3, data=b"SAME")
    two = FakeAttachment(att_id="2", filename="b.png", content_type="image/png", size=3, data=b"SAME")
    meta = asyncio.run(store_attachments(str(tmp_path), "170000000000000000", [one, two]))
    assert meta[0]["path"] == meta[1]["path"]  # один и тот же sha256 → один файл

    off = asyncio.run(store_attachments("", "170000000000000000", [one]))  # MEDIA_DIR не задан → не пишем
    assert off[0]["stored"] is False and off[0]["path"] == ""


# M08: два одновременных сохранения одного digest — итоговый файл цел, .part не разделяются.
def test_store_attachments_concurrent_same_digest(tmp_path):
    async def slow_read_factory(att_id, delay):
        class Slow(FakeAttachment):
            async def read(self, use_cached=False):
                await asyncio.sleep(delay)
                return self._data

        return Slow(att_id=att_id, filename=f"{att_id}.png", content_type="image/png",
                    size=4, data=b"DUEL")

    async def main():
        a, b = await slow_read_factory("1", 0.0), await slow_read_factory("2", 0.01)
        return await asyncio.gather(
            store_attachments(str(tmp_path), "170000000000000000", [a]),
            store_attachments(str(tmp_path), "170000000000000000", [b]),
        )

    meta_a, meta_b = asyncio.run(main())
    assert meta_a[0]["stored"] and meta_b[0]["stored"]
    assert meta_a[0]["path"] == meta_b[0]["path"]
    assert (tmp_path / meta_a[0]["path"]).read_bytes() == b"DUEL"
    # мусорных .part файлов не остаётся
    assert not list(tmp_path.rglob("*.part"))


# L05: при нехватке места новые вложения не качаются и не пишутся; метаданные
# честно несут причину; существующий архив остаётся нетронутым.
def test_store_attachments_disk_quota_stops_new_uploads(tmp_path, monkeypatch):
    # уже сохранённый файл архива
    old = tmp_path / "170000000000000000" / "2026-09" / "old.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"KEEP")

    class NoRead(FakeAttachment):
        async def read(self, use_cached=False):
            raise AssertionError("при низкой квоте скачивание начинаться не должно")

    monkeypatch.setattr(media_module.shutil, "disk_usage",
                        lambda _p: SimpleNamespace(free=1024, total=1 << 30, used=0))
    meta = asyncio.run(store_attachments(str(tmp_path), "170000000000000000",
                                         [NoRead(att_id="7", filename="n.png", content_type="image/png", size=4, data=b"N")],
                                         min_free_bytes=4096))
    assert meta[0]["stored"] is False
    assert meta[0]["storeSkipReason"] == "disk-quota-low"
    assert meta[0]["url"]  # ссылка Discord остаётся запасным путём
    assert old.read_bytes() == b"KEEP"  # архив не тронут
    assert not list(tmp_path.rglob("*.part"))


# L05: min_free_bytes=0 — проверка выключена (вообще не дёргаем statfs).
def test_store_attachments_disk_quota_disabled(tmp_path, monkeypatch):
    def boom(_p):
        raise AssertionError("при min_free_bytes=0 диск проверять не нужно")

    monkeypatch.setattr(media_module.shutil, "disk_usage", boom)
    png = FakeAttachment(att_id="8", filename="ok.png", content_type="image/png", size=2, data=b"OK")
    meta = asyncio.run(store_attachments(str(tmp_path), "170000000000000000", [png], min_free_bytes=0))
    assert meta[0]["stored"] is True
    assert "storeSkipReason" not in meta[0]


# L05: места достаточно — обычной записи ничего не мешает, причина не проставляется.
def test_store_attachments_disk_room_ok_no_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(media_module.shutil, "disk_usage",
                        lambda _p: SimpleNamespace(free=10 * 4096, total=1 << 30, used=0))
    png = FakeAttachment(att_id="6", filename="fine.png", content_type="image/png", size=3, data=b"FIN")
    meta = asyncio.run(store_attachments(str(tmp_path), "170000000000000000", [png], min_free_bytes=4096))
    assert meta[0]["stored"] is True
    assert "storeSkipReason" not in meta[0]


# L03: зависшее скачивание прерывается таймаутом — мета остаётся без файла, gateway не блокируется.
def test_store_attachments_download_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(media_module, "DOWNLOAD_TIMEOUT_S", 0.05)

    class Hanging(FakeAttachment):
        async def read(self, use_cached=False):
            await asyncio.sleep(5)
            return self._data

    hang = Hanging(att_id="9", filename="h.png", content_type="image/png", size=1, data=b"H")
    ok = FakeAttachment(att_id="10", filename="o.png", content_type="image/png", size=1, data=b"O")
    meta = asyncio.run(store_attachments(str(tmp_path), "170000000000000000", [hang, ok]))
    assert meta[0]["stored"] is False  # таймаут → только метаданные
    assert meta[1]["stored"] is True  # соседнее скачивание не пострадало
