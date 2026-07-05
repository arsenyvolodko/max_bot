"""
Кеш media-токенов Max (аналог переиспользования file_id в Telegram).

После загрузки файла в Max возвращается переиспользуемый token. Мы кешируем
соответствие url -> {token, type} в JSON-файле. URL с бэкенда контент-адресуемый
(содержит хеш контента), поэтому одинаковый url == одинаковый контент: при
совпадении url отдаём картинку по токену без скачивания с бэка и без повторной
загрузки в Max. При промахе — качаем и грузим один раз.
"""
import config  # noqa: F401  — ПЕРВЫМ: применяет SSL-патч до импорта maxapi

import asyncio
import json
import os
from typing import Optional
from uuid import uuid4

from maxapi import Bot
from maxapi.types.attachments.upload import AttachmentPayload, AttachmentUpload
from maxapi.types.input_media import InputMediaBuffer
from maxapi.utils.message import process_input_media

import api
from config import MEDIA_CACHE_FILE

# url -> {"token": str, "type": str}
_cache: dict[str, dict] = {}
_loaded = False
_lock = asyncio.Lock()


def _load() -> None:
    global _cache, _loaded
    if _loaded:
        return
    if os.path.exists(MEDIA_CACHE_FILE):
        try:
            with open(MEDIA_CACHE_FILE, encoding="utf-8") as f:
                _cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            _cache = {}
    _loaded = True


def _save() -> None:
    tmp = f"{MEDIA_CACHE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False)
    os.replace(tmp, MEDIA_CACHE_FILE)


def _build(entry: dict) -> AttachmentUpload:
    """Собрать attachment-по-токену для отправки/редактирования сообщения."""
    return AttachmentUpload(
        type=entry["type"], payload=AttachmentPayload(token=entry["token"])
    )


async def get_attachment(
    bot: Bot, url: str, filename: Optional[str] = None, force: bool = False
) -> AttachmentUpload:
    """Attachment для media по URL с переиспользованием токена.

    Args:
        force: пропустить кеш и перезалить (например, если токен протух).
    """
    _load()
    async with _lock:
        if not force and url in _cache:
            return _build(_cache[url])

        # промах кеша: скачиваем с бэка и грузим в Max один раз
        buffer = await api.fetch_bytes(url)
        att = await process_input_media(
            base_connection=bot,
            bot=bot,
            att=InputMediaBuffer(buffer=buffer, filename=filename or uuid4().hex),
        )
        type_value = att.type.value if hasattr(att.type, "value") else att.type
        entry = {"token": att.payload.token, "type": type_value}
        _cache[url] = entry
        _save()
        return _build(entry)


def invalidate(url: str) -> None:
    """Выбросить запись из кеша (если Max отверг токен как невалидный)."""
    _load()
    if url in _cache:
        del _cache[url]
        _save()
