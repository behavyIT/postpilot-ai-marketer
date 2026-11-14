# Образ приложения: один образ для api, bot, worker и migrate (разные команды в compose.yaml).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir "uv==0.13.7"

WORKDIR /app

# Сначала только зависимости — слой кешируется, пока не меняется uv.lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser /app

USER appuser
