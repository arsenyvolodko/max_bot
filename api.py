"""Асинхронный клиент к бэкенду мероприятия (Django REST)."""
import config  # noqa: F401  — ПЕРВЫМ: применяет SSL-патч до импорта aiohttp

from typing import Any, Optional

import aiohttp

from config import BACKEND_URL

_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    """Лениво создаёт общую сессию (внутри работающего event loop)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(base_url=BACKEND_URL)
    return _session


async def close_session() -> None:
    if _session and not _session.closed:
        await _session.close()


async def get_or_create_user(user_id: int) -> tuple[int, dict[str, Any]]:
    """POST /api/users/ — get_or_create.

    Returns:
        (status, user). status == 201 если создан, 200 если уже был.
        user = {"user_id": int, "city": {"id", "name"} | None}
    """
    session = await _get_session()
    async with session.post("/api/users/", json={"user_id": user_id}) as r:
        return r.status, await r.json()


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
