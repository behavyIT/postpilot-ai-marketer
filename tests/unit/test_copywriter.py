from app.agents.copywriter import Copywriter, NumberedFact, PostBrief
from app.ai.llm.base import LLMRequest, ModelProfile
from app.ai.llm.fake import FakeLLMProvider
from app.ai.llm.limiter import NullLimiter
from app.ai.llm.service import CallContext, LLMService
from app.ai.llm.usage import MemoryUsageSink
from app.domain.platforms import TELEGRAM

BRIEF = PostBrief(
    category="Меню",
    goal="sales",
    topic="Осенний раф",
    title="Раф с корицей",
    brief="Рассказать о новинке",
    cta="Заходи после пар",
    hashtags=["#раф"],
    visual_idea="чашка рафа",
)
FACTS = [NumberedFact(1, "Средний чек 350 ₽"), NumberedFact(2, "Работаем навынос у КФУ")]


def writer(provider: FakeLLMProvider) -> Copywriter:
    return Copywriter(
        LLMService(
            provider,
            models={ModelProfile.SMART: "ultra", ModelProfile.FAST: "lite"},
            limiter=NullLimiter(),
            usage=MemoryUsageSink(),
        )
    )


async def write(provider: FakeLLMProvider, reference_text: str | None = None):
    return await writer(provider).write(
        BRIEF,
        spec=TELEGRAM,
        strategy=None,
        facts=FACTS,
        preferences=["без эмодзи"],
        instructions=None,
        ctx=CallContext(purpose="t"),
        reference_text=reference_text,
    )


async def test_post_gets_hashtags_at_the_end_and_unknown_facts_filtered() -> None:
    provider = FakeLLMProvider(
        {
            "body": "<b>Раф с корицей</b>\nЗаходи после пар — средний чек 350 ₽.\n#раф #кофе",
            "hashtags": ["раф", "кофе", "#Раф"],
            "used_fact_ids": [1, 1, 99],
        }
    )
    post = await write(provider)

    assert post.text == "<b>Раф с корицей</b>\nЗаходи после пар — средний чек 350 ₽.\n\n#раф #кофе"
    assert post.used_fact_numbers == [1]
    assert post.warnings == []
    prompt = provider.requests[0].messages[1].content
    assert "[1] Средний чек 350 ₽" in prompt
    assert "- без эмодзи" in prompt
    assert "до 900 символов" in prompt


async def test_invented_numbers_and_superlatives_become_warnings() -> None:
    provider = FakeLLMProvider(
        {"body": "<b>Лучший раф</b>\nВсего 199 ₽ и скидка 30%!", "hashtags": [], "used_fact_ids": []}
    )
    post = await write(provider, reference_text="Средний чек 350 ₽")
    assert any("«199 ₽»" in w and "«30%»" in w for w in post.warnings)
    assert any("«лучший»" in w for w in post.warnings)


async def test_long_post_is_shortened_once_and_bad_markup_removed() -> None:
    seen: list[LLMRequest] = []

    def responder(request: LLMRequest) -> dict[str, object]:
        seen.append(request)
        if len(seen) == 1:
            return {"body": "<div>" + "очень длинно " * 120 + "</div>", "hashtags": [], "used_fact_ids": []}
        return {"body": "<div>Коротко о рафе</div>", "hashtags": [], "used_fact_ids": [2]}

    post = await write(FakeLLMProvider(responder=responder))

    assert len(seen) == 2
    assert "Сократи пост до 900" in seen[1].messages[-1].content
    assert post.text == "Коротко о рафе\n\n#раф"  # <div> убран; хештеги взяты из пункта плана
    assert post.used_fact_numbers == [2]


async def test_fact_numbers_never_leak_into_post_text() -> None:
    provider = FakeLLMProvider(
        {
            "body": "<b>Обжарка</b>\nСобственное производство [2]. Проверяем партии [1, 2].",
            "hashtags": [],
            "used_fact_ids": [1, 2],
        }
    )
    post = await write(provider)
    assert post.text.startswith("<b>Обжарка</b>\nСобственное производство. Проверяем партии.")
    assert post.warnings == []
