# AI-маркетолог

Автономный AI-маркетолог в Telegram: изучает бизнес, предлагает стратегию, готовит контент-планы и публикации, отправляет их на согласование и публикует одобренное по расписанию.

Архитектура и решения — [docs/01-technical-analysis.md](docs/01-technical-analysis.md).

## Что нужно

- macOS или Linux, Docker (на Mac — OrbStack или Docker Desktop)
- [uv](https://docs.astral.sh/uv/) — Python 3.13 он поставит сам
- Токен Telegram-бота от @BotFather и ключ GigaChat API (тариф Freemium для физлиц)

## Первый запуск

```bash
cp .env.example .env        # впишите TELEGRAM_BOT_TOKEN и GIGACHAT_AUTH_KEY
docker compose up -d        # PostgreSQL (pgvector) на 5433 и Redis на 6380
uv sync                     # зависимости
uv run alembic upgrade head # схема базы
```

Запуск процессов (каждый в своём терминале):

```bash
uv run python -m app.bot                                              # Telegram-бот (polling)
uv run uvicorn app.main:create_app --factory --port 8000              # API: /health, /ready
uv run taskiq worker app.workers.broker:broker app.workers.tasks      # фоновые задачи
```

Или всё в контейнерах: `docker compose --profile app up -d --build`.

## Проверки

```bash
uv run pytest            # тесты (нужны контейнеры postgres и redis)
uv run ruff check .      # линтер
uv run mypy app          # типы
uv run python scripts/spike_gigachat.py   # проверка ключа GigaChat и возможностей API
```

## Сертификат Минцифры

API GigaChat использует сертификат НУЦ Минцифры. Корневой и промежуточный сертификаты лежат в `certs/` и подключаются только к клиенту GigaChat — системные настройки доверия не меняются.
