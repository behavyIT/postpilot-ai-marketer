"""Хеш содержимого версии: то, что одобрил владелец, должно совпасть с тем, что уходит в канал."""

from __future__ import annotations

import hashlib
import json


def content_hash(body: str, media_sha256: str | None) -> str:
    """Хеш текста (уже с хештегами) и картинки. Меняется при любой правке текста или замене картинки."""
    payload = json.dumps({"body": body, "media": media_sha256}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
