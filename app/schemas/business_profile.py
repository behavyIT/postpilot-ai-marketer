"""Структура профиля бизнеса (хранится в business_profile_versions.data)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.enums import FactStatus, ProfileField, SourceKind

FIELD_LABELS: dict[ProfileField, str] = {
    ProfileField.COMPANY_NAME: "Название",
    ProfileField.INDUSTRY: "Сфера деятельности",
    ProfileField.PRODUCTS_SERVICES: "Продукты и услуги",
    ProfileField.TARGET_AUDIENCE: "Целевая аудитория",
    ProfileField.GEOGRAPHY: "География",
    ProfileField.UNIQUE_SELLING_POINTS: "Уникальные торговые предложения",
    ProfileField.COMPETITIVE_ADVANTAGES: "Конкурентные преимущества",
    ProfileField.PRICING: "Цены",
    ProfileField.COMMUNICATION_STYLE: "Стиль коммуникации",
    ProfileField.PRODUCT_BENEFITS: "Выгоды для клиента",
    ProfileField.CUSTOMER_PAINS: "Боли и потребности клиентов",
    ProfileField.FAQ: "Частые вопросы",
    ProfileField.ADVERTISING_RESTRICTIONS: "Ограничения в рекламе",
    ProfileField.CONTACTS: "Контакты",
}

# Без этих пунктов стратегию не построить — о них спрашиваем в первую очередь.
KEY_FIELDS = (
    ProfileField.COMPANY_NAME,
    ProfileField.PRODUCTS_SERVICES,
    ProfileField.TARGET_AUDIENCE,
    ProfileField.GEOGRAPHY,
    ProfileField.COMPETITIVE_ADVANTAGES,
)


class SourceRef(BaseModel):
    kind: SourceKind
    id: UUID
    label: str
    quote: str


class ProfileFieldValue(BaseModel):
    value: str | None = None
    status: FactStatus = FactStatus.MISSING
    sources: list[SourceRef] = Field(default_factory=list)


class BusinessProfileData(BaseModel):
    summary: str | None = None
    fields: dict[ProfileField, ProfileFieldValue] = Field(default_factory=dict)

    def get(self, name: ProfileField) -> ProfileFieldValue:
        return self.fields.get(name) or ProfileFieldValue()

    @property
    def filled(self) -> int:
        return sum(1 for f in ProfileField if self.get(f).value)

    @property
    def completeness(self) -> int:
        return round(self.filled * 100 / len(ProfileField))

    @property
    def missing(self) -> list[ProfileField]:
        return [f for f in ProfileField if not self.get(f).value]
