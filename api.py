"""Асинхронный клиент к бэкенду мероприятия (Django REST)."""
import config  # noqa: F401  — ПЕРВЫМ: применяет SSL-патч до импорта aiohttp

import asyncio
import functools
import logging
from typing import Any, Optional

import aiohttp

from config import BACKEND_URL

log = logging.getLogger("max_bot.api")

# Ошибки бэкенда, которые обязаны ловить хендлеры. asyncio.TimeoutError НЕ
# наследуется от aiohttp.ClientError — без него таймаут бэка роняет хендлер и
# пользователь не получает ни ответа, ни сообщения об ошибке.
BACKEND_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)

# Без явного таймаута aiohttp ждёт ответа 5 минут (дефолт total=300) — всё это
# время бот «не реагирует» на нажатие. Держим короткие рамки: бэк локальный.
TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_connect=5, sock_read=10)

# Сколько раз повторить запрос при таймауте/обрыве соединения. Все ручки
# идемпотентны (GET и get_or_create-подобные POST), повтор безопасен.
RETRIES = 1
RETRY_DELAY = 0.5

_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    """Лениво создаёт общую сессию (внутри работающего event loop)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(base_url=BACKEND_URL, timeout=TIMEOUT)
    return _session


def _with_retry(func):
    """Повторить запрос при таймауте/обрыве соединения (сетевой флап бэка)."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        last_error: Exception
        for attempt in range(RETRIES + 1):
            try:
                return await func(*args, **kwargs)
            except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as e:
                last_error = e
                if attempt < RETRIES:
                    log.warning(
                        "%s: %s (%r), повтор %d/%d",
                        func.__name__, type(e).__name__, e, attempt + 1, RETRIES,
                    )
                    await asyncio.sleep(RETRY_DELAY)
        raise last_error

    return wrapper


async def close_session() -> None:
    if _session and not _session.closed:
        await _session.close()


@_with_retry
async def get_or_create_user(user_id: int) -> tuple[int, dict[str, Any]]:
    """POST /api/users/ — get_or_create.

    Returns:
        (status, user). status == 201 если создан, 200 если уже был.
        user = {"user_id": int, "city": {"id", "name"} | None}
    """
    session = await _get_session()
    async with session.post("/api/users/", json={"user_id": user_id}) as r:
        return r.status, await r.json()


@_with_retry
async def get_cities() -> list[dict[str, Any]]:
    """GET /api/cities/ — список городов [{"id", "name"}, ...]."""
    session = await _get_session()
    async with session.get("/api/cities/") as r:
        r.raise_for_status()
        return await r.json()


async def get_city(city_id: int) -> Optional[dict[str, Any]]:
    """Найти город по id (через список /api/cities/). None, если не найден."""
    for city in await get_cities():
        if city["id"] == city_id:
            return city
    return None


@_with_retry
async def join_city(user_id: int, city_id: int) -> dict[str, Any]:
    """POST /api/users/{user_id}/city/ — привязать пользователя к городу.

    Returns: user с заполненным city ({"id", "name"}).
    """
    session = await _get_session()
    async with session.post(
        f"/api/users/{user_id}/city/", json={"city_id": city_id}
    ) as r:
        r.raise_for_status()
        return await r.json()


@_with_retry
async def list_users(city_id: Optional[int] = None) -> list[dict[str, Any]]:
    """GET /api/users/list/ — список пользователей.

    Args:
        city_id: если задан, фильтрует по городу (?city_id=<id>);
            None — все пользователи (для рассылки «все города»).

    Returns:
        [{"user_id": int, "is_manager": bool, "city": {"id", "name"}}, ...]
    """
    session = await _get_session()
    params = {"city_id": city_id} if city_id is not None else None
    async with session.get("/api/users/list/", params=params) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_bytes(url: str) -> bytes:
    """Скачать файл с бэкенда по (обычно абсолютному) URL медиа.

    media-ссылки приходят на тот же BACKEND_URL (например map_schema), который
    серверам Max недоступен (127.0.0.1) — поэтому качаем сами и грузим в Max.
    """
    session = await _get_session()
    # сессия создана с base_url, поэтому для абсолютной ссылки на тот же хост
    # отрезаем префикс и ходим относительным путём.
    path = url[len(BACKEND_URL):] if url.startswith(BACKEND_URL) else url
    async with session.get(path) as r:
        r.raise_for_status()
        return await r.read()


async def get_program(city_id: int) -> tuple[int, Optional[dict[str, Any]]]:
    """GET /api/cities/{city_id}/program/.

    Returns:
        (200, program) если есть, (404, None) если программы ещё нет.
    """
    session = await _get_session()
    async with session.get(f"/api/cities/{city_id}/program/") as r:
        if r.status == 404:
            return 404, None
        r.raise_for_status()
        return r.status, await r.json()
