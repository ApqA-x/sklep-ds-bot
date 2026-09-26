import asyncio
from datetime import datetime, timezone

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
