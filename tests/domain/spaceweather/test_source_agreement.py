"""Source agreement foundation (FN-47) — контракт ``source_agreement`` и его
адаптер для архивных продуктов Механизма 1.

Полностью детерминированы, без сети и без хранилища (main-prompt.md §9):
:func:`compute_source_agreement` — чистая функция над готовыми
:class:`SourceAssessment`, поэтому сценарии ниже строятся вручную, без
реальных архивных ответов. Приёмка FN-47 п.3 — дословно три сценария:
согласие, разногласие, недостаток данных (нет источников/один источник).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.spaceweather.archive_assessment import (
    ArchiveWindowAssessment,
    source_assessment_for_agreement,
)
from src.domain.spaceweather.source_agreement import (
    DuplicateSourceError,
    SourceAssessment,
    compute_source_agreement,
)

UTC = timezone.utc
WINDOW_START = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2024, 5, 10, 6, 0, tzinfo=UTC)
AS_OF = WINDOW_END


def _archive_assessment(
    *, status: str, source_id: str, record_ids: tuple[str, ...] = ()
) -> ArchiveWindowAssessment:
    return ArchiveWindowAssessment(
        status=status,  # type: ignore[arg-type]
        source_id=source_id,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        as_of=AS_OF,
        horizon_end=None,
        beyond_horizon=False,
        critical_gap=status == "INSUFFICIENT_DATA",
        coverage_fraction=1.0,
        events=(),
        unresolved_open_events=(),
        record_ids=record_ids,
        notes=(f"synthetic {status} for {source_id}",),
    )


# ---------------------------------------------------------------------------
# Приёмка п.3 — agreement
# ---------------------------------------------------------------------------


def test_two_independent_sources_agreeing_is_consistent() -> None:
    result = compute_source_agreement(
        [
            SourceAssessment(source_id="donki", classification="EVENT_PRESENT"),
            SourceAssessment(source_id="swpc-forecast-discussion", classification="EVENT_PRESENT"),
        ]
    )
    assert result.agreement == "CONSISTENT"
    assert result.applicable_classifications == frozenset({"EVENT_PRESENT"})
    # Provenance каждого источника сохранена целиком, не свёрнута в один вывод.
    assert {a.source_id for a in result.assessments} == {"donki", "swpc-forecast-discussion"}


def test_three_independent_sources_all_agreeing_is_still_consistent() -> None:
    result = compute_source_agreement(
        [
            SourceAssessment(source_id="a", classification="NO_EVENT_DETECTED"),
            SourceAssessment(source_id="b", classification="NO_EVENT_DETECTED"),
            SourceAssessment(source_id="c", classification="NO_EVENT_DETECTED"),
        ]
    )
    assert result.agreement == "CONSISTENT"


# ---------------------------------------------------------------------------
# Приёмка п.3 — disagreement
# ---------------------------------------------------------------------------


def test_two_independent_sources_disagreeing_is_conflict() -> None:
    result = compute_source_agreement(
        [
            SourceAssessment(source_id="donki", classification="EVENT_PRESENT"),
            SourceAssessment(
                source_id="swpc-forecast-discussion", classification="NO_EVENT_DETECTED"
            ),
        ]
    )
    assert result.agreement == "CONFLICT"
    assert result.applicable_classifications == frozenset({"EVENT_PRESENT", "NO_EVENT_DETECTED"})
    # Оба источника видны в результате — ни один не выбран молча как "лучший".
    assert len(result.assessments) == 2


def test_conflict_survives_a_third_agreeing_source_outvoting_is_not_a_thing() -> None:
    """Постановка FN-47 не вводит голосование/большинство — разные
    классификации среди применимых оценок это всегда CONFLICT, независимо от
    того, сколько источников на какой стороне."""
    result = compute_source_agreement(
        [
            SourceAssessment(source_id="a", classification="EVENT_PRESENT"),
            SourceAssessment(source_id="b", classification="NO_EVENT_DETECTED"),
            SourceAssessment(source_id="c", classification="NO_EVENT_DETECTED"),
        ]
    )
    assert result.agreement == "CONFLICT"


# ---------------------------------------------------------------------------
# Приёмка п.3 — missing/one source
# ---------------------------------------------------------------------------


def test_no_sources_is_insufficient_data() -> None:
    result = compute_source_agreement([])
    assert result.agreement == "INSUFFICIENT_DATA"
    assert result.assessments == ()
    assert result.applicable_classifications == frozenset()


def test_one_applicable_source_is_insufficient_data() -> None:
    result = compute_source_agreement(
        [SourceAssessment(source_id="donki", classification="EVENT_PRESENT")]
    )
    assert result.agreement == "INSUFFICIENT_DATA"


def test_one_applicable_and_one_inapplicable_source_is_still_insufficient_data() -> None:
    """Источник без decision-relevant классификации (``classification=None``
    — например сам «оценить невозможно») не может стать вторым ПРИМЕНИМЫМ
    источником — main-prompt.md §2 запрещает превращать отказ/пробел в
    благоприятный (или любой другой) вывод."""
    result = compute_source_agreement(
        [
            SourceAssessment(source_id="donki", classification="EVENT_PRESENT"),
            SourceAssessment(source_id="swpc-forecast-discussion", classification=None),
        ]
    )
    assert result.agreement == "INSUFFICIENT_DATA"
    # ...но provenance неприменимого источника всё равно сохранена.
    assert len(result.assessments) == 2
    assert any(a.source_id == "swpc-forecast-discussion" and a.classification is None
               for a in result.assessments)


def test_two_inapplicable_sources_is_insufficient_data() -> None:
    result = compute_source_agreement(
        [
            SourceAssessment(source_id="a", classification=None),
            SourceAssessment(source_id="b", classification=None),
        ]
    )
    assert result.agreement == "INSUFFICIENT_DATA"
    assert result.applicable_classifications == frozenset()


# ---------------------------------------------------------------------------
# Не выбирать источник молча / не путать источники друг с другом
# ---------------------------------------------------------------------------


def test_duplicate_source_id_raises_instead_of_silently_picking_one() -> None:
    with pytest.raises(DuplicateSourceError):
        compute_source_agreement(
            [
                SourceAssessment(source_id="donki", classification="EVENT_PRESENT"),
                SourceAssessment(source_id="donki", classification="NO_EVENT_DETECTED"),
            ]
        )


# ---------------------------------------------------------------------------
# Адаптер архивных продуктов (archive_assessment.py) в общий вид FN-47
# ---------------------------------------------------------------------------


def test_adapter_maps_event_present_to_its_own_classification() -> None:
    assessment = _archive_assessment(
        status="EVENT_PRESENT", source_id="nasa-donki-notifications", record_ids=("r1", "r2")
    )
    mapped = source_assessment_for_agreement(assessment)
    assert mapped.source_id == "nasa-donki-notifications"
    assert mapped.classification == "EVENT_PRESENT"
    assert mapped.record_ids == ("r1", "r2")


def test_adapter_maps_no_event_detected_to_its_own_classification() -> None:
    assessment = _archive_assessment(
        status="NO_EVENT_DETECTED", source_id="noaa-swpc-forecast-discussion-archive"
    )
    mapped = source_assessment_for_agreement(assessment)
    assert mapped.classification == "NO_EVENT_DETECTED"


def test_adapter_maps_insufficient_data_to_no_classification_but_keeps_provenance() -> None:
    assessment = _archive_assessment(
        status="INSUFFICIENT_DATA",
        source_id="noaa-swpc-forecast-discussion-archive",
        record_ids=("r9",),
    )
    mapped = source_assessment_for_agreement(assessment)
    assert mapped.classification is None
    assert mapped.record_ids == ("r9",)
    assert mapped.notes == assessment.notes


def test_two_archive_products_via_adapter_disagreeing_is_conflict() -> None:
    """Сквозной сценарий: два реальных архивных продукта Механизма 1 (DONKI и
    SWPC Forecast Discussion — независимость обоих задокументирована в
    ``sources.yaml``) через адаптер дают CONFLICT, если один нашёл событие,
    а другой доказанно его не нашёл."""
    donki = _archive_assessment(status="EVENT_PRESENT", source_id="nasa-donki-notifications")
    swpc = _archive_assessment(
        status="NO_EVENT_DETECTED", source_id="noaa-swpc-forecast-discussion-archive"
    )
    result = compute_source_agreement(
        [source_assessment_for_agreement(donki), source_assessment_for_agreement(swpc)]
    )
    assert result.agreement == "CONFLICT"
