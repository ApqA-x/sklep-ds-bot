"""Хранение вложений сообщений: файловая система сервера (MEDIA_DIR) + метаданные в Mongo.

Бот скачивает только картинки (белый список типов, лимит по размеру) в
<MEDIA_DIR>/<guildId>/<YYYY-MM>/<sha256>.<ext>; имена — хэш содержимого, поэтому
дубликаты не занимают место, а угадать путь снаружи невозможно. Остальные вложения
записываются в метаданные как есть (stored=false, url — ссылка Discord, протухает).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 МБ
DOWNLOAD_TIMEOUT_S = 60.0  # L03: потолок на одно скачивание
DOWNLOAD_KINDS = {"image"}  # что реально храним на диске
_MAX_CONCURRENT_DOWNLOADS = 4
_SAFE_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
_SNOWFLAKE_RE = re.compile(r"^\d{5,25}$")
_semaphore: asyncio.Semaphore | None = None


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


def relative_path(guild_id: str, sent_at: datetime | None, digest: str, ext: str) -> str:
    moment = sent_at or datetime.now(timezone.utc)
    return f"{guild_id}/{moment:%Y-%m}/{digest}{ext}"


async def store_attachments(media_dir: str, guild_id: str, attachments: Any, sent_at: datetime | None = None) -> list[dict]:
    """Превращает discord.Attachment'ы в метаданные для chat_messages.attachments.

    media_dir пуст -> ничего не скачиваем, только метаданные (stored=false).
    Ошибки одного вложения не должны ронять запись сообщения.
    """
    meta_list: list[dict] = []
    if not attachments:
        return meta_list
    root = Path(media_dir) if media_dir else None
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
