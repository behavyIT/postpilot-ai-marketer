from uuid import uuid4

from app.agents.business_analyst import ProfileSynthesis, SynthesizedField
from app.db.models import ProfileFact
from app.domain.enums import FactStatus, ProfileField, SourceKind
from app.services.profile_analysis import build_profile_fields, is_supported


def fact(
    field: ProfileField, value: str, quote: str, status: FactStatus = FactStatus.INFERRED
) -> ProfileFact:
    return ProfileFact(
        project_id=uuid4(),
        field=field,
        value=value,
        quote=quote,
        source_kind=SourceKind.NOTE if status is FactStatus.CONFIRMED else SourceKind.WEBSITE_PAGE,
        source_id=uuid4(),
        source_label="источник",
        status=status,
    )


def synthesis(*fields: SynthesizedField) -> ProfileSynthesis:
    return ProfileSynthesis(summary="", fields=list(fields), questions=[])


def test_support_check_tolerates_word_forms_but_rejects_new_claims() -> None:
    facts = [fact(ProfileField.INDUSTRY, "Кофейня, франшиза", "франшиза Правда кофе")]
    assert is_supported("Кофейня по франшизе «Правда кофе»", facts)
    assert not is_supported("Сеть ресторанов премиального уровня с доставкой", facts)
    assert not is_supported("", facts)


def test_unreferenced_but_supported_wording_is_accepted() -> None:
    numbered = {
        1: fact(ProfileField.INDUSTRY, "Кофейня, франшиза", "франшиза Правда кофе", FactStatus.CONFIRMED),
        2: fact(ProfileField.INDUSTRY, "Кофейни, розничная продажа кофе", "розничная продажа кофе"),
    }
    fields = build_profile_fields(
        synthesis(
            SynthesizedField(
                field="industry", value="Кофейня по франшизе, розничная продажа кофе", fact_ids=[]
            )
        ),
        numbered,
    )
    value = fields[ProfileField.INDUSTRY]
    assert value.value == "Кофейня по франшизе, розничная продажа кофе"
    assert value.status is FactStatus.CONFIRMED


def test_unsupported_wording_falls_back_to_distinct_facts() -> None:
    numbered = {
        1: fact(ProfileField.INDUSTRY, "Кофейня, франшиза", "франшиза", FactStatus.CONFIRMED),
        2: fact(ProfileField.INDUSTRY, "Кофейня", "кофейня"),  # содержится в первом — повтор
        3: fact(ProfileField.INDUSTRY, "Школа бариста", "школа бариста"),
    }
    fields = build_profile_fields(
        synthesis(
            SynthesizedField(field="industry", value="Международный холдинг премиум-класса", fact_ids=[])
        ),
        numbered,
    )
    assert fields[ProfileField.INDUSTRY].value == "Кофейня, франшиза; Школа бариста"


def test_fields_without_facts_are_never_filled() -> None:
    fields = build_profile_fields(
        synthesis(SynthesizedField(field="pricing", value="Средний чек 1000 ₽", fact_ids=[99])), {}
    )
    assert fields == {}
