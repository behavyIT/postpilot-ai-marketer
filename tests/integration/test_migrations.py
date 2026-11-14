import pytest
from alembic import command

from tests.conftest import run_alembic

pytestmark = pytest.mark.integration


def test_migrations_roundtrip_and_match_models(migrated_database_url: str) -> None:
    """Миграции откатываются и применяются заново, а схема совпадает с моделями."""
    run_alembic(command.downgrade, migrated_database_url, "base")
    run_alembic(command.upgrade, migrated_database_url, "head")
    # command.check падает, если модели содержат изменения, которых нет в миграциях.
    run_alembic(command.check, migrated_database_url)
