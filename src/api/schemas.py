"""Pydantic-схемы API S1-07 (.ai/backend-prompt.md §6).

``CalculationRequest`` — та же форма, что ``contracts/request.schema.json``
(единственный источник истины о форме, см. ``contracts/README.md``). Часть
правил запроса не выражается в JSON Schema (сравнение дат) и реализована
здесь валидаторами Pydantic — ровно то, что ``contracts/request.schema.json``
и ``contracts/README.md`` документируют как обязанность сервисного слоя:

- ``as_of`` обязателен только для ``mode = historical_forecast``, запрещён для
  ``current``/``historical_analysis``;
- ``as_of <= start_at`` (иначе отсечение оказывается позже начала окна —
  ретроспективный разбор по факту, выданный за прогноз из прошлого);
- для ``historical_*`` режимов ``start_at`` обязан попадать в обязательный
  исторический период 1 мая — 30 июня 2024 (границы включены).

Наблюдение по освещённости (``lighting_constraint``) на этом этапе
отклоняется явной ошибкой валидации, а не тихо принимается: ``src/domain/
lighting`` ещё не реализован (зона 2, main-prompt.md §11 «реализуется после
механизма 2 и отбрасывается первой при нехватке времени»), а
``contracts/result.schema.json`` → ``window.lighting.status`` не имеет
значения «не реализовано» — только ``not_requested``/``satisfied``/
``violated``. Подставить ``satisfied`` без проверки было бы тем самым ложным
благоприятным выводом, который main-prompt.md §2 прямо запрещает для отказов
источников; тот же принцип применён здесь к непроверяемому ограничению.
Честный выбор в рамках этой задачи — отказать запросу явно, а не придумать
непроверенный вердикт.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Mode = Literal["current", "historical_analysis", "historical_forecast"]

# Единственный источник истины для границ обязательного исторического периода
# — contracts/README.md, раздел «Обязательный исторический период» (граница
# не выражается в JSON Schema, задокументирована и реализована здесь).
ARCHIVE_START = datetime(2024, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
ARCHIVE_END = datetime(2024, 6, 30, 23, 59, 59, 999999, tzinfo=timezone.utc)

ALGORITHM_VERSION = "0.1.0"


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be timezone-aware ISO 8601 with explicit offset "
            "(.ai/main-prompt.md §1); naive datetimes are rejected"
        )
    return value


class LightingConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires_sunlight: bool


class CalculationRequest(BaseModel):
    """Тело запроса на расчёт — форма ``contracts/request.schema.json``."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode
    start_at: datetime
    duration_hours: Annotated[float, Field(ge=1, le=8)]
    search_window_hours: Annotated[float, Field(ge=0, le=24)]
    as_of: datetime | None = None
    lighting_constraint: LightingConstraint | None = None

    @model_validator(mode="after")
    def _check_temporal_rules(self) -> CalculationRequest:
        _require_aware(self.start_at, "start_at")
        if self.as_of is not None:
            _require_aware(self.as_of, "as_of")

        if self.mode == "historical_forecast":
            if self.as_of is None:
                raise ValueError(
                    "as_of is required for mode=historical_forecast "
                    "(contracts/request.schema.json)"
                )
        elif self.as_of is not None:
            raise ValueError(
                f"as_of is not allowed for mode={self.mode!r} "
                "(contracts/request.schema.json)"
            )

        if self.as_of is not None and self.as_of > self.start_at:
            raise ValueError(
                "as_of must be <= start_at: a cutoff after the window start turns a "
                "strict historical forecast into a retrospective analysis of what "
                "already happened (contracts/README.md, «Три режима и as_of»)"
            )

        if self.mode in ("historical_analysis", "historical_forecast"):
            if not (ARCHIVE_START <= self.start_at <= ARCHIVE_END):
                raise ValueError(
                    f"start_at must fall within the supported historical period "
                    f"{ARCHIVE_START.isoformat()} .. {ARCHIVE_END.isoformat()} "
                    f"(contracts/README.md, «Обязательный исторический период»); "
                    f"got {self.start_at.isoformat()}"
                )

        if self.lighting_constraint is not None:
            raise ValueError(
                "lighting_constraint is not supported yet: src/domain/lighting is "
                "not implemented in this service version, and the result contract "
                "has no 'not implemented' value for window.lighting.status — "
                "fabricating satisfied/violated without a real check would be "
                "exactly the false favorable conclusion main-prompt.md §2 forbids "
                "for source failures; omit lighting_constraint for now"
            )

        return self

    @property
    def search_end_at(self) -> datetime:
        return self.start_at + timedelta(hours=self.search_window_hours)

    @property
    def calc_end_at(self) -> datetime:
        """Верхняя граница расчётного интервала (до 32 часов от ``start_at``,
        contracts/README.md «Часы и период поиска»)."""
        return self.search_end_at + timedelta(hours=self.duration_hours)

    def to_contract_dict(self) -> dict[str, object]:
        """Сериализует запрос ровно в форму ``contracts/request.schema.json``:
        ``as_of`` отсутствует как ключ (а не ``null``), когда не задан."""
        payload: dict[str, object] = {
            "mode": self.mode,
            "start_at": iso_utc(self.start_at),
            "duration_hours": self.duration_hours,
            "search_window_hours": self.search_window_hours,
        }
        if self.as_of is not None:
            payload["as_of"] = iso_utc(self.as_of)
        if self.lighting_constraint is not None:
            payload["lighting_constraint"] = {
                "requires_sunlight": self.lighting_constraint.requires_sunlight
            }
        return payload


def iso_utc(value: datetime) -> str:
    """ISO 8601 UTC с суффиксом ``Z`` — единая сериализация времени для API/результата."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TaskCreatedResponse(BaseModel):
    task_id: str
    status: Literal["pending"] = "pending"


JobStatus = Literal["pending", "running", "done", "failed"]


class ApiErrorBody(BaseModel):
    code: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: JobStatus
    result_id: str | None
    error: ApiErrorBody | None


class SourceStatusResponse(BaseModel):
    source_id: str
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_message: str | None
    frozen: bool
    quota_limited: bool


class ResultListItem(BaseModel):
    result_id: str
    computed_at: str
    mode: Mode
    as_of: str | None
    recommendation_status: str


__all__ = [
    "ALGORITHM_VERSION",
    "ARCHIVE_END",
    "ARCHIVE_START",
    "ApiErrorBody",
    "CalculationRequest",
    "JobStatus",
    "LightingConstraint",
    "ResultListItem",
    "SourceStatusResponse",
    "TaskCreatedResponse",
    "TaskStatusResponse",
    "iso_utc",
]
