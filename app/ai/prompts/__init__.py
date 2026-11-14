"""Версионированные шаблоны промптов (*.md рядом с этим файлом)."""

from __future__ import annotations

from functools import cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent


@cache
def load_prompt(name: str) -> str:
    """Текст промпта без строк-комментариев, начинающихся с «<!--»."""
    text = (_DIR / f"{name}.md").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("<!--")]
    return "\n".join(lines).strip()
