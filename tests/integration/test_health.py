import httpx
import pytest

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration


async def _get(settings: Settings, path: str) -> httpx.Response:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)


async def test_health_is_ok(settings: Settings) -> None:
    response = await _get(settings, "/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_ready_checks_postgres_and_redis(settings: Settings, migrated_database_url: str) -> None:
    test_settings = settings.model_copy(update={"database_url": migrated_database_url})
    response = await _get(test_settings, "/ready")
    assert response.status_code == 200, response.json()
    assert response.json()["checks"] == {"postgres": "ok", "redis": "ok"}


async def test_ready_reports_unavailable_dependency(settings: Settings) -> None:
    broken = settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})
    response = await _get(broken, "/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["redis"].startswith("error")
