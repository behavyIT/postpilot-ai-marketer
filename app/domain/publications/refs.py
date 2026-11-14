"""Ссылка на конкретную версию поста — так её несут кнопки и превью."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PostRef:
    variant_id: UUID
    version_no: int

    def value(self) -> str:
        """Компактная форма для callback_data: «<hex>.<версия>» (≤ 36 символов)."""
        return f"{self.variant_id.hex}.{self.version_no}"

    @classmethod
    def parse(cls, value: str) -> PostRef:
        """«hex.версия[.что-то ещё]» → PostRef; ValueError, если значение испорчено."""
        hex_id, _, rest = value.partition(".")
        return cls(UUID(hex=hex_id), int(rest.split(".")[0]))
