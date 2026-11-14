from uuid import uuid4

from app.agents.orchestrator.profile_view import analysis_summary, format_profile
from app.domain.enums import FactStatus, ProfileField, SourceKind
from app.schemas.business_profile import BusinessProfileData, ProfileFieldValue, SourceRef


def make_profile(value_length: int = 50) -> BusinessProfileData:
    ref = SourceRef(kind=SourceKind.NOTE, id=uuid4(), label="Сообщение", quote="цитата")
    data = BusinessProfileData(summary="Кофейня <b>в центре</b>")
    data.fields[ProfileField.COMPANY_NAME] = ProfileFieldValue(
        value="Зерно", status=FactStatus.CONFIRMED, sources=[ref]
    )
    data.fields[ProfileField.PRODUCTS_SERVICES] = ProfileFieldValue(
        value="к" * value_length, status=FactStatus.INFERRED, sources=[ref]
    )
    return data


def test_profile_shows_marks_missing_fields_questions_and_escapes_html() -> None:
    text = format_profile("Зерно & Ко", make_profile(), version=2, questions=["Кто клиенты?"])

    assert "Профиль бизнеса «Зерно &amp; Ко»" in text
    assert "заполнено 2 из 14 · версия 2" in text
    assert "&lt;b&gt;в центре&lt;/b&gt;" in text
    assert "✅ <b>Название:</b> Зерно" in text
    assert "🔎 <b>Продукты и услуги:</b>" in text
    assert "❓ Нет данных: сфера деятельности" in text
    assert "1. Кто клиенты?" in text


def test_long_profile_fits_telegram_limit() -> None:
    data = make_profile(value_length=600)
    for name in ProfileField:
        data.fields.setdefault(name, ProfileFieldValue(value="ш" * 600, status=FactStatus.INFERRED))
    text = format_profile("Зерно", data, version=1, questions=["вопрос " * 40] * 5)
    assert len(text) <= 3900


def test_analysis_summary_reports_rejections_failures_and_suspicious_sources() -> None:
    text = analysis_summary(
        websites=[("https://pravdacoffee.ru/", 12)],
        documents=["Прайс.pdf"],
        facts_added=45,
        facts_rejected=11,
        failures=["Документ «скан.pdf»: возможно, это скан"],
        suspicious=["Сайт: Акции"],
    )
    assert "сайт pravdacoffee.ru (12 стр.), «Прайс.pdf»" in text
    assert "Нашёл новых фактов о бизнесе: 45" in text
    assert "11 отбросил" in text
    assert "⚠️ Документ «скан.pdf»" in text
    assert "команды для ИИ" in text
