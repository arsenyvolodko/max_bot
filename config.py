"""Загрузка конфигурации из окружения."""
import os
import ssl

import certifi
from dotenv import load_dotenv

# macOS + сборка Python с python.org: aiohttp создаёт SSL-контексты (в т.ч. в
# голых ClientSession() внутри maxapi для загрузки файлов) без системных
# CA-сертификатов и падает с SSLCertVerificationError. Глобально подставляем
# certifi во ВСЕ создаваемые контексты. Делать это нужно до первого обращения
# к сети (config импортируется первым). На Linux/проде безвредно.
_orig_create_default_context = ssl.create_default_context


def _create_default_context_with_certifi(*args, **kwargs):
    kwargs.setdefault("cafile", certifi.where())
    return _orig_create_default_context(*args, **kwargs)


ssl.create_default_context = _create_default_context_with_certifi

load_dotenv()

TOKEN = os.getenv("MAX_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Не задан MAX_BOT_TOKEN. Положи токен от MasterBot в файл .env"
    )

# Базовый адрес бэкенда мероприятия (Django REST).
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")

# Файл персистентного кеша media-токенов Max (url -> token).
MEDIA_CACHE_FILE = os.getenv(
    "MEDIA_CACHE_FILE",
    os.path.join(os.path.dirname(__file__), ".media_cache.json"),
)
