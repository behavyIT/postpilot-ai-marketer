from datetime import UTC, date, datetime
from uuid import uuid4

from app.agents.orchestrator.content_view import plan_reply, post_preview, strategy_reply
from app.agents.orchestrator.replies import Action
from app.db.models import ContentPlan, Publication
from app.domain.enums import DocumentStatus
from tests.unit.test_strategy_and_plan import strategy


def publication(title: str, when: datetime | None, **kw: str) -> Publication:
    return Publication(
        id=uuid4(),
        project_id=uuid4(),
        planned_at=when,
        category=kw.get("category", "Полезное"),
        goal=kw.get("goal", "education"),
        topic=title,
        title=title,
        brief="",
        cta="",
        hashtags=[],
        visual_idea=kw.get("visual", ""),
    )


def test_strategy_draft_has_approve_button_and_escapes_text() -> None:
    data = strategy(summary="Кофе <для> студентов")
    reply = strategy_reply("Зерно", data, strategy_id="s-1", version=2, status=DocumentStatus.DRAFT)

    assert "Стратегия продвижения «Зерно»" in reply.text
    assert "вариант 2 · черновик" in reply.text
    assert "Кофе &lt;для&gt; студентов" in reply.text
    assert "• Полезное — 50% (польза)" in reply.text
    assert "Telegram: 7 в неделю, 09:00, 18:00" in reply.text
    actions = [b.action for row in reply.buttons for b in row]
    assert actions == [Action.STRATEGY_APPROVE, Action.STRATEGY_REDO, Action.PLAN_CREATE]
    assert reply.buttons[0][0].value == "s-1"


def test_approved_strategy_has_no_approve_button() -> None:
    reply = strategy_reply("Зерно", strategy(), strategy_id="s", version=1, status=DocumentStatus.APPROVED)
    assert "утверждена" in reply.text
    assert Action.STRATEGY_APPROVE not in [b.action for row in reply.buttons for b in row]


def test_plan_is_grouped_by_local_day_and_marks_written_posts() -> None:
    plan = ContentPlan(
        id=uuid4(),
        project_id=uuid4(),
        period_start=date(2026, 11, 17),
        period_end=date(2026, 11, 23),
        status=DocumentStatus.DRAFT,
    )
    first = publication("Как выбрать зерно", datetime(2026, 11, 17, 6, 0, tzinfo=UTC))
    second = publication("Раф с корицей", datetime(2026, 11, 17, 15, 0, tzinfo=UTC), category="Меню")
    third = publication("Команда", datetime(2026, 11, 18, 7, 0, tzinfo=UTC))

    reply = plan_reply(
        "Зерно", plan, [first, second, third], timezone="Europe/Moscow", written={first.id}, notes=["заметка"]
    )

    assert "17.11 – 23.11 · 3 публикаций в Telegram · черновик" in reply.text
    assert (
        "<b>Вт 17.11</b>\n09:00 · Полезное · «Как выбрать зерно» ✍️\n18:00 · Меню · «Раф с корицей»"
        in reply.text
    )
    assert "<b>Ср 18.11</b>" in reply.text
    assert "ℹ️ заметка" in reply.text
    actions = [b.action for row in reply.buttons for b in row]
    assert actions == [Action.PLAN_APPROVE, Action.PLAN_REDO, Action.PLAN_WRITE]


def test_post_preview_shows_meta_visual_idea_and_warnings() -> None:
    pub = publication("Раф", datetime(2026, 11, 17, 6, 0, tzinfo=UTC), visual="чашка <рафа>")
    reply = post_preview(
        pub,
        "<b>Раф</b>\nТекст",
        warnings=["Проверьте цифры: «20%»"],
        timezone="Europe/Moscow",
        index=1,
        total=7,
    )
    assert reply.text.startswith("<b>Пост 1/7</b> · Вт 17.11, 09:00 · Полезное · цель: польза")
    assert "<b>Раф</b>\nТекст" in reply.text  # текст поста — как будет опубликован
    assert "Идея картинки: чашка &lt;рафа&gt;" in reply.text
    assert "• Проверьте цифры: «20%»" in reply.text
