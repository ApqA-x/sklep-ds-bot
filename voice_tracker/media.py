"""Хранение вложений сообщений: файловая система сервера (MEDIA_DIR) + метаданные в Mongo.

Бот скачивает только картинки (белый список типов, лимит по размеру) в
<MEDIA_DIR>/<guildId>/<YYYY-MM>/<sha256>.<ext>; имена — хэш содержимого, поэтому
дубликаты не занимают место, а угадать путь снаружи невозможно. Остальные вложения
записываются в метаданные как есть (stored=false, url — ссылка Discord, протухает).

T16 (L05): перед скачиванием проверяется свободное место (MEDIA_MIN_FREE_BYTES).
При нехватке новые вложения НЕ сохраняются (stored=false с причиной в метаданных,
Discord-URL остаётся), но существующий архив не трогается никогда — удаление
старых данных возможно только по решению D07 (см. docs/runbook-retention.md).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 МБ
DOWNLOAD_TIMEOUT_S = 60.0  # L03: потолок на одно скачивание
DOWNLOAD_KINDS = {"image"}  # что реально храним на диске
_MAX_CONCURRENT_DOWNLOADS = 4
DEFAULT_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB — порог остановки новых загрузок
_DISK_WARN_INTERVAL_S = 300.0  # не шуметь в логе на каждом вложении
_SAFE_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
_SNOWFLAKE_RE = re.compile(r"^\d{5,25}$")
_semaphore: asyncio.Semaphore | None = None
_last_disk_warn = 0.0


def _download_semaphore() -> asyncio.Semaphore:
    # лениво: Semaphore привязан к запущенному event loop'у сервиса
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)
    return _semaphore

_KIND_BY_PREFIX = (
    ("image/", "image"),
    ("video/", "video"),
    ("audio/", "audio"),
)


def classify(content_type: str, filename: str) -> str:
    ctype = (content_type or "").lower()
    for prefix, kind in _KIND_BY_PREFIX:
        if ctype.startswith(prefix):
            return kind
    name = (filename or "").lower()
    if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if name.endswith((".mp4", ".webm", ".mov")):
        return "video"
    if name.endswith((".mp3", ".ogg", ".wav")):
        return "audio"
    return "file"


def _extension(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if _SAFE_EXT_RE.match(ext) else ".bin"


def _disk_room_ok(root: Path, min_free_bytes: int) -> bool:
    """L05: есть ли место под НОВЫЕ вложения. Ошибки statfs не блокируют запись —
    реперная точка отказа от диска это OSError при сохранении ниже."""
    if min_free_bytes <= 0:
        return True
    probe = root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        logger.warning("media disk check failed for %s", probe, exc_info=True)
        return True
    if usage.free >= min_free_bytes:
        return True
    global _last_disk_warn
    now = time.monotonic()
    if now - _last_disk_warn >= _DISK_WARN_INTERVAL_S:
        _last_disk_warn = now
        logger.warning(
            "media disk quota low: free=%d bytes < min=%d — новые вложения не сохраняются,"
            " существующий архив не трогается (решение об удалении: D07, docs/runbook-retention.md)",
            usage.free,
            min_free_bytes,
        )
    return False


def relative_path(guild_id: str, sent_at: datetime | None, digest: str, ext: str) -> str:
    moment = sent_at or datetime.now(timezone.utc)
    return f"{guild_id}/{moment:%Y-%m}/{digest}{ext}"


async def store_attachments(
    media_dir: str,
    guild_id: str,
    attachments: Any,
    sent_at: datetime | None = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> list[dict]:
    """Превращает discord.Attachment'ы в метаданные для chat_messages.attachments.

    media_dir пуст -> ничего не скачиваем, только метаданные (stored=false).
    min_free_bytes: L05 — при нехватке места новые картинки НЕ скачиваем, но
    существующий архив не трогаем (meta.stored=false, meta.storeSkipReason).
    Ошибки одного вложения не должны ронять запись сообщения.
    """
    meta_list: list[dict] = []
    if not attachments:
        return meta_list
    root = Path(media_dir) if media_dir else None
    # L05: одна проверка диска на сообщение (дешевле, чем на каждое вложение).
    disk_room = True if root is None else _disk_room_ok(root, min_free_bytes)
    safe_guild = guild_id if _SNOWFLAKE_RE.match(guild_id or "") else "unknown"
    for attachment in attachments:
        filename = str(getattr(attachment, "filename", "") or "")
        content_type = str(getattr(attachment, "content_type", "") or "")
        kind = classify(content_type, filename)
        size = int(getattr(attachment, "size", 0) or 0)
        meta: dict[str, Any] = {
            "id": str(getattr(attachment, "id", "") or ""),
            "filename": filename[:255],
            "contentType": content_type[:127],
            "size": size,
            "kind": kind,
            "path": "",
            "stored": False,
            "url": str(getattr(attachment, "proxy_url", "") or getattr(attachment, "url", "") or "")[:512],
        }
        meta_list.append(meta)
        if root is None or kind not in DOWNLOAD_KINDS or size <= 0 or size > MAX_ATTACHMENT_BYTES:
            continue
        if not disk_room:
            # L05: сохраняем метаданные с URL Discord и честной причиной —
            # удаление существующего архива возможно только по решению D07.
            meta["storeSkipReason"] = "disk-quota-low"
            continue
        try:
            async with _download_semaphore():
                # L03: одно медленное скачивание не держит весь gateway
                data = await asyncio.wait_for(
                    attachment.read(use_cached=False), timeout=DOWNLOAD_TIMEOUT_S
                )
        except Exception:
            logger.warning("attachment download failed id=%s guild=%s", meta["id"], safe_guild, exc_info=True)
            continue
        if len(data) > MAX_ATTACHMENT_BYTES:
            continue
        digest = hashlib.sha256(data).hexdigest()
        rel = relative_path(safe_guild, sent_at, digest, _extension(filename))
        target = root / rel
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                # M08: временный файл уникален на скачивание — два параллельных
                # save одного digest не пишут в общий .part; os.replace атомарен.
                tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
                await asyncio.to_thread(tmp.write_bytes, data)
                await asyncio.to_thread(os.replace, tmp, target)
        except OSError:
            logger.warning("attachment save failed path=%s", rel, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        meta["path"] = rel
        meta["stored"] = True
    return meta_list
