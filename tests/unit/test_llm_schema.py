from enum import StrEnum

from pydantic import BaseModel

from app.ai.llm.schema import extract_json, to_llm_schema


class Color(StrEnum):
    RED = "red"
    BLUE = "blue"


class Inner(BaseModel):
    name: str


class Outer(BaseModel):
    color: Color
    count: int | None = None
    inner: Inner
    tags: list[str]


def test_schema_has_no_refs_or_null_variants() -> None:
    schema = to_llm_schema(Outer)
    text = str(schema)
    assert "$ref" not in text
    assert "$defs" not in text
    assert "anyOf" not in text
    assert "title" not in schema
    assert schema["properties"]["count"] == {"type": "integer"}
    assert schema["properties"]["color"]["enum"] == ["red", "blue"]
    assert schema["properties"]["inner"]["properties"]["name"]["type"] == "string"
    assert set(schema["required"]) == {"color", "inner", "tags"}


def test_extract_json_strips_fences_and_chatter() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Вот ответ: {"a": 1} — готово') == '{"a": 1}'
    assert extract_json('  {"a": 1}  ') == '{"a": 1}'
    assert extract_json("нет json") == "нет json"
