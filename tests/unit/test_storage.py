from pathlib import Path

import pytest

from app.integrations.storage.local import LocalFileStorage


async def test_save_read_delete(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    await storage.save("projects/p1/uploads/a.pdf", b"%PDF-data")

    assert (tmp_path / "projects/p1/uploads/a.pdf").read_bytes() == b"%PDF-data"
    assert await storage.read("projects/p1/uploads/a.pdf") == b"%PDF-data"
    assert not list(tmp_path.rglob("*.tmp"))

    await storage.delete("projects/p1/uploads/a.pdf")
    await storage.delete("projects/p1/uploads/a.pdf")  # повторное удаление не падает
    assert not (tmp_path / "projects/p1/uploads/a.pdf").exists()


async def test_keys_cannot_escape_storage_root(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "files")
    with pytest.raises(ValueError):
        await storage.save("../../etc/passwd", b"x")
