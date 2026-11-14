"""Подготовка JSON Schema для модели и разбор её JSON-ответа.

Pydantic генерирует схемы с $ref/$defs и anyOf для Optional-полей. Не все
провайдеры понимают такие конструкции, поэтому для модели схема упрощается:
ссылки разворачиваются, «X или null» превращается в «X, необязательное поле».
Строгую проверку ответа всё равно делает Pydantic по исходной модели.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from pydantic import BaseModel

_DROP_KEYS = {"title", "default"}
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def to_llm_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Упрощённая JSON Schema модели без $ref, anyOf с null и служебных полей."""
    raw = model.model_json_schema()
    defs: dict[str, Any] = raw.pop("$defs", {})
    simplified = _simplify(raw, defs)
    if not isinstance(simplified, dict):  # pragma: no cover — корень схемы всегда объект
        raise TypeError("Корень схемы должен быть объектом")
    return simplified


def _simplify(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_simplify(item, defs) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        resolved = copy.deepcopy(defs[name])
        extra = {k: v for k, v in node.items() if k != "$ref"}
        return _simplify({**resolved, **extra}, defs)

    if "anyOf" in node:
        variants = [v for v in node["anyOf"] if v != {"type": "null"}]
        if len(variants) == 1:
            merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **variants[0]}
            return _simplify(merged, defs)

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        result[key] = _simplify(value, defs)
    return result


def extract_json(text: str) -> str:
    """Достать JSON из ответа модели: убрать ```-обёртку и текст вокруг объекта."""
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if stripped.startswith("{"):
        return stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def schema_prompt(model: type[BaseModel]) -> str:
    """Текстовое описание схемы для инструкции в промпте (на случай, если провайдер её игнорирует)."""
    return json.dumps(to_llm_schema(model), ensure_ascii=False)
