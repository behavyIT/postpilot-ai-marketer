"""Проверки живости и готовности."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Процесс жив."""
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    """Процесс готов обслуживать запросы: доступны PostgreSQL и Redis."""
    engine: AsyncEngine = request.app.state.engine
    redis: Redis = request.app.state.redis
    checks: dict[str, str] = {}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # проверка готовности не должна падать сама
        checks["postgres"] = f"error: {type(exc).__name__}"

    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"

    is_ready = all(value == "ok" for value in checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if is_ready else "unavailable", "checks": checks}
