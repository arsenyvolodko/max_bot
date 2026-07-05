FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

# Poetry для установки зависимостей из lock-файла
RUN pip install "poetry==${POETRY_VERSION}"

# Сначала только манифесты — кешируем слой с зависимостями
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

# Затем исходники
COPY . .

CMD ["python", "main.py"]