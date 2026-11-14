from app.agents.orchestrator.intents import Intent, IntentClassifier, IntentResult
from app.ai.llm.base import LLMMessage, ModelProfile
from app.ai.llm.fake import FakeLLMProvider
from app.ai.llm.limiter import NullLimiter
from app.ai.llm.service import CallContext, LLMService
from app.ai.llm.usage import MemoryUsageSink


def test_unknown_intent_value_becomes_unknown() -> None:
    result = IntentResult.model_validate({"intent": "make_coffee", "confidence": 0.9})
    assert result.intent is Intent.UNKNOWN


def test_values_are_normalised() -> None:
    result = IntentResult.model_validate(
        {
            "intent": " Create_Plan ",
            "confidence": "1.7",
            "period_days": "400",
            "topic": "   ",
            "project_name": "null",
            "when": "завтра   в 12:00",
        }
    )
    assert result.intent is Intent.CREATE_PLAN
    assert result.confidence == 1.0
    assert result.period_days == 90
    assert result.topic is None
    assert result.project_name is None
    assert result.when == "завтра в 12:00"


def test_bad_numbers_do_not_break_parsing() -> None:
    result = IntentResult.model_validate(
        {"intent": "analytics", "confidence": "высокая", "period_days": "месяц"}
    )
    assert result.confidence == 0.0
    assert result.period_days is None


async def test_classifier_puts_system_prompt_first_and_uses_smart_model() -> None:
    provider = FakeLLMProvider({"intent": "create_plan", "confidence": 0.95, "period_days": 7})
    llm = LLMService(
        provider,
        models={ModelProfile.SMART: "ultra", ModelProfile.FAST: "lite"},
        limiter=NullLimiter(),
        usage=MemoryUsageSink(),
    )
    history = [LLMMessage("user", "Сделай план"), LLMMessage("assistant", "На какой период?")]

    result = await IntentClassifier(llm).classify("на неделю", history=history, ctx=CallContext(purpose="t"))

    assert result.intent is Intent.CREATE_PLAN
    request = provider.requests[0]
    assert request.model == "ultra"
    assert request.messages[0].role == "system"
    assert "распознавания намерений" in request.messages[0].content
    assert [m.content for m in request.messages[1:]] == ["Сделай план", "На какой период?", "на неделю"]
