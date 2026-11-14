import pytest
from pydantic import BaseModel, Field

from app.ai.llm.base import LLMConfigError, LLMMessage, LLMOutputError, LLMRequest, ModelProfile
from app.ai.llm.fake import FakeLLMProvider
from app.ai.llm.gigachat import _to_gigachat_messages
from app.ai.llm.limiter import NullLimiter
from app.ai.llm.service import CallContext, LLMService
from app.ai.llm.usage import MemoryUsageSink
from app.domain.enums import LLMCallStatus

CTX = CallContext(purpose="test")
MODELS = {ModelProfile.SMART: "smart-model", ModelProfile.FAST: "fast-model"}


class Idea(BaseModel):
    title: str = Field(min_length=3)
    hashtags: list[str] = Field(max_length=3)


def make_service(provider: FakeLLMProvider) -> tuple[LLMService, MemoryUsageSink]:
    usage = MemoryUsageSink()
    return LLMService(provider, models=MODELS, limiter=NullLimiter(), usage=usage), usage


async def test_structured_returns_validated_object_and_records_usage() -> None:
    provider = FakeLLMProvider({"title": "Осенний раф", "hashtags": ["#кофе"]})
    service, usage = make_service(provider)

    idea = await service.structured([LLMMessage("user", "идея")], Idea, ctx=CTX)

    assert idea == Idea(title="Осенний раф", hashtags=["#кофе"])
    request = provider.requests[0]
    assert request.model == "fast-model"
    assert request.json_schema is not None
    assert [r.status for r in usage.records] == [LLMCallStatus.OK]
    assert usage.records[0].input_tokens == 10


async def test_schema_is_not_duplicated_in_prompt_when_provider_supports_it() -> None:
    provider = FakeLLMProvider({"title": "Тест", "hashtags": []}, supports_json_schema=True)
    service, _ = make_service(provider)
    await service.structured([LLMMessage("user", "x")], Idea, ctx=CTX)
    assert [m.role for m in provider.requests[0].messages] == ["user"]


async def test_schema_is_added_to_prompt_for_providers_without_native_support() -> None:
    provider = FakeLLMProvider({"title": "Тест", "hashtags": []}, supports_json_schema=False)
    service, _ = make_service(provider)
    await service.structured([LLMMessage("user", "x")], Idea, ctx=CTX)
    messages = provider.requests[0].messages
    assert messages[-1].role == "system"
    assert "JSON Schema" in messages[-1].content


async def test_invalid_output_gets_one_repair_attempt() -> None:
    provider = FakeLLMProvider(
        {"title": "Ок", "hashtags": ["#1", "#2", "#3", "#4"]},  # слишком коротко и много хештегов
        '```json\n{"title": "Осенний раф", "hashtags": ["#1"]}\n```',
    )
    service, usage = make_service(provider)

    idea = await service.structured([LLMMessage("user", "идея")], Idea, ctx=CTX)

    assert idea.title == "Осенний раф"
    repair = provider.requests[1]
    assert repair.messages[-2].role == "assistant"
    assert "не прошёл проверку" in repair.messages[-1].content
    assert [r.status for r in usage.records] == [
        LLMCallStatus.OK,
        LLMCallStatus.INVALID_OUTPUT,
        LLMCallStatus.OK,
    ]


async def test_second_invalid_output_raises_friendly_error() -> None:
    provider = FakeLLMProvider("не json", "опять не json")
    service, usage = make_service(provider)
    with pytest.raises(LLMOutputError):
        await service.structured([LLMMessage("user", "x")], Idea, ctx=CTX)
    assert usage.records[-1].status == LLMCallStatus.INVALID_OUTPUT


async def test_empty_answer_is_retried_once() -> None:
    provider = FakeLLMProvider("", "Готовый текст")
    service, _ = make_service(provider)
    assert await service.text([LLMMessage("user", "x")], ctx=CTX) == "Готовый текст"
    assert len(provider.requests) == 2


async def test_provider_error_is_recorded_and_propagated() -> None:
    provider = FakeLLMProvider(LLMConfigError("плохой ключ"))
    service, usage = make_service(provider)
    with pytest.raises(LLMConfigError):
        await service.text([LLMMessage("user", "x")], ctx=CTX)
    assert usage.records[0].status == LLMCallStatus.ERROR
    assert "плохой ключ" in (usage.records[0].error or "")


def test_gigachat_gets_single_leading_system_message() -> None:
    messages = [
        LLMMessage("system", "Правила"),
        LLMMessage("user", "Привет"),
        LLMMessage("assistant", "Здравствуйте"),
        LLMMessage("system", "Схема ответа"),
        LLMMessage("user", "План"),
    ]
    converted = _to_gigachat_messages(messages)
    roles = [m.role.value if hasattr(m.role, "value") else m.role for m in converted]
    assert roles == ["system", "user", "assistant", "user"]
    assert converted[0].content == "Правила\n\nСхема ответа"


def test_request_defaults() -> None:
    request = LLMRequest(model="m", messages=[])
    assert request.json_schema is None
    assert request.max_tokens == 1024
