from datetime import UTC, date, datetime

from app.agents.content_planner import normalize_hashtags, similar, target_counts, validate_plan
from app.agents.strategist import Strategist, normalize_strategy
from app.ai.llm.base import LLMRequest, ModelProfile
from app.ai.llm.fake import FakeLLMProvider
from app.ai.llm.limiter import NullLimiter
from app.ai.llm.service import CallContext, LLMService
from app.ai.llm.usage import MemoryUsageSink
from app.domain.enums import ContentGoal, FactStatus, Platform, ProfileField
from app.schemas.business_profile import BusinessProfileData, ProfileFieldValue
from app.schemas.content import AudienceSegment, PlanItem, PlatformPlan, Rubric, StrategyData


def strategy(**overrides: object) -> StrategyData:
    data = StrategyData(
        summary="Продвигаем кофейню у КФУ",
        positioning="Быстрый вкусный кофе навынос",
        audience_segments=[AudienceSegment(name="Студенты", needs="быстро", message="успей до пары")],
        goals=["Узнаваемость у КФУ"],
        tone_of_voice="Дружелюбно, на «ты»",
        rubrics=[
            Rubric(name="Полезное", goal="education", share_percent=50, description="", example_topics=[]),
            Rubric(name="Меню", goal="sales", share_percent=30, description="", example_topics=[]),
            Rubric(name="О нас", goal="loyalty", share_percent=20, description="", example_topics=[]),
        ],
        platforms=[
            PlatformPlan(platform="telegram", posts_per_week=7, best_times=["09:00", "18:00"], notes="")
        ],
        content_rules=["Без превосходных степеней"],
        kpis=["Реакции в Telegram"],
    )
    return data.model_copy(update=overrides)


def test_normalize_strategy_fixes_shares_platforms_times_and_goals() -> None:
    raw = strategy(
        rubrics=[
            Rubric(name="Полезное", goal="education", share_percent=80, description="", example_topics=[]),
            Rubric(name="Акции", goal="money", share_percent=80, description="", example_topics=[]),
        ],
        platforms=[
            PlatformPlan(platform="vk", posts_per_week=5, best_times=["10:00"], notes=""),
            PlatformPlan(
                platform="telegram", posts_per_week=40, best_times=["9:5", "25:00", "8:30"], notes=""
            ),
        ],
    )
    data = normalize_strategy(raw, [Platform.TELEGRAM])

    assert len(data.rubrics) == 3  # недостающая рубрика добавлена
    assert sum(r.share_percent for r in data.rubrics) == 100
    assert data.rubrics[1].goal == ContentGoal.ENGAGEMENT  # неизвестная цель заменена
    assert [p.platform for p in data.platforms] == ["telegram"]  # VK не подключён — убран
    assert data.platforms[0].posts_per_week == 14
    assert data.platforms[0].best_times == ["08:30"]


async def test_strategist_prompt_contains_profile_preferences_and_instructions() -> None:
    seen: list[LLMRequest] = []

    def responder(request: LLMRequest) -> dict[str, object]:
        seen.append(request)
        return strategy().model_dump()

    llm = LLMService(
        FakeLLMProvider(responder=responder),
        models={ModelProfile.SMART: "ultra", ModelProfile.FAST: "lite"},
        limiter=NullLimiter(),
        usage=MemoryUsageSink(),
    )
    profile = BusinessProfileData(summary="Кофейня у КФУ")
    profile.fields[ProfileField.GEOGRAPHY] = ProfileFieldValue(value="Казань", status=FactStatus.CONFIRMED)

    await Strategist(llm).create(
        profile,
        platforms=[Platform.TELEGRAM],
        preferences=["без эмодзи"],
        previous=strategy(summary="Старый вариант"),
        instructions="упор на студентов",
        ctx=CallContext(purpose="t"),
    )

    user_text = seen[0].messages[1].content
    assert "География (со слов владельца): Казань" in user_text
    assert "- без эмодзи" in user_text
    assert "упор на студентов" in user_text
    assert "Старый вариант" in user_text
    assert "Доступные площадки: telegram" in user_text


def item(day: str, time: str, title: str, rubric: str = "Полезное", **kw: str) -> PlanItem:
    return PlanItem(
        date=day,
        time=time,
        platform=kw.get("platform", "telegram"),
        rubric=rubric,
        goal=kw.get("goal", "education"),
        topic=title,
        title=title,
        brief="о чём пост",
        cta="Заходи",
        hashtags=["кофе", "#Кофе", "#осень", "#кзн", "#раф", "#лишний"],
        visual_idea="чашка",
    )


def test_plan_validation_fixes_dates_repeats_rubrics_and_slots() -> None:
    items = [
        item("2026-11-17", "09:00", "Как выбрать зерно для дома"),
        item("2026-11-17", "09:00", "Осенний раф с корицей", rubric="меню"),  # то же время — сдвиг на 3 часа
        item("2026-11-17", "20:00", "Третий пост за день"),  # больше двух в день — убираем
        item("2026-11-30", "09:00", "Вне периода"),
        item("2026-11-18", "99:99", "Как выбрать зерно для дома"),  # повтор темы
        item("2026-11-19", "10:00", "Наша команда бариста", goal="money"),
        item("2026-11-20", "10:00", "Пять ошибок при заварке кофе"),  # было в недавних публикациях
    ]
    result = validate_plan(
        items,
        strategy=strategy(),
        start=date(2026, 11, 17),
        end=date(2026, 11, 23),
        timezone="Europe/Moscow",
        counts={"telegram": 5},
        recent_topics=["Пять ошибок при заварке кофе дома"],
    )

    titles = [i.title for i in result.items]
    assert titles == ["Как выбрать зерно для дома", "Осенний раф с корицей", "Наша команда бариста"]
    first, second, third = result.items
    assert first.planned_at == datetime(2026, 11, 17, 6, 0, tzinfo=UTC)  # 09:00 МСК
    assert second.planned_at == datetime(2026, 11, 17, 9, 0, tzinfo=UTC)  # сдвиг на 3 часа
    assert second.category == "Меню"  # рубрика сопоставлена без учёта регистра
    assert third.goal is ContentGoal.EDUCATION  # неизвестная цель → цель рубрики «Полезное»
    assert first.hashtags == ["#кофе", "#осень", "#кзн", "#раф"]
    assert any("вне периода" in n for n in result.notes)
    assert any("повторяющихся" in n for n in result.notes)
    assert any("3 публикаций из 5" in n for n in result.notes)


def test_missing_title_falls_back_to_topic() -> None:
    raw = item("2026-11-17", "09:00", "Тема без заголовка").model_copy(update={"title": ""})
    result = validate_plan(
        [raw],
        strategy=strategy(),
        start=date(2026, 11, 17),
        end=date(2026, 11, 23),
        timezone="Europe/Moscow",
        counts={"telegram": 1},
        recent_topics=[],
    )
    assert result.items[0].title == "Тема без заголовка"


def test_plan_item_tolerates_missing_optional_fields() -> None:
    parsed = PlanItem.model_validate(
        {
            "date": "2026-11-17",
            "time": "09:00",
            "platform": "telegram",
            "rubric": "Меню",
            "goal": "sales",
            "topic": "Раф",
            "brief": "о рафе",
        }
    )
    assert (parsed.title, parsed.cta, parsed.hashtags, parsed.visual_idea) == ("", "", [], "")


def test_helpers() -> None:
    assert target_counts(strategy(), 14) == {"telegram": 14}
    assert target_counts(strategy(), 3) == {"telegram": 3}
    assert similar("Пять ошибок при выборе кофе", "5 ошибок при выборе кофе дома")
    assert not similar("Осенний раф с корицей", "Как выбрать зерно для дома")
    assert normalize_hashtags(["кофе", "#Кофе", "  ", "#кофе-брейк"], limit=5) == ["#кофе", "#кофебрейк"]
