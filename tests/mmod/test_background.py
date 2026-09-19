"""Тесты нормировки ``ratio_to_background`` и порогов 1.2/2 (FN-39, S2-08).

Независимая проверка численной части (.ai/main-prompt.md §9 «численная
часть проверяется независимым примером»): контрольные строки самого
первичного документа (``2024-05-05T14:00Z``/``2024-06-09T23:00Z``, сверены
посимвольно с реальным файлом в ``tests/sources/test_mmod.py``) плюс прямая
арифметика порогов main-prompt.md §11, не пересчёт через код модуля.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.mmod.background import (
    BackgroundNode,
    UnsupportedMmodBackgroundRecordError,
    assess_mmod_background,
    background_nodes_from_records,
    classify_level,
    ratio_to_background,
)
from src.sources.mmod import SOURCE_ID, VALUE_UNIT

UTC = timezone.utc


def _node(hour: int, factor: float, *, record_id: str | None = None) -> BackgroundNode:
    return BackgroundNode(
        at=datetime(2024, 5, 5, hour, tzinfo=UTC),
        factor_105j=factor,
        record_id=record_id or f"rec-{hour}",
    )


class TestRatioToBackground:
    def test_zero_factor_is_exactly_background_unity(self) -> None:
        assert ratio_to_background(0.0) == 1.0

    def test_known_document_values(self) -> None:
        """2024-05-05T14:00Z и 2024-06-09T23:00Z — реальные строки
        документа (tests/sources/test_mmod.py), не выдуманные примеры."""
        assert ratio_to_background(0.3165592) == pytest.approx(1.3165592)
        assert ratio_to_background(0.5820643) == pytest.approx(1.5820643)


class TestClassifyLevel:
    """Пороги main-prompt.md §11: Фон < 1.2, Повышенный [1.2, 2], Выраженный > 2."""

    @pytest.mark.parametrize(
        ("ratio", "expected"),
        [
            (0.0, "background"),
            (1.0, "background"),
            (1.1999999, "background"),
            (1.2, "elevated"),
            (1.5, "elevated"),
            (2.0, "elevated"),  # «до 2» включительно — см. background.py docstring
            (2.0000001, "pronounced"),
            (5.0, "pronounced"),
        ],
    )
    def test_thresholds(self, ratio: float, expected: str) -> None:
        assert classify_level(ratio) == expected


class TestBackgroundNodesFromRecords:
    def _record(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "record_id": "rec-1",
            "source_id": SOURCE_ID,
            "record_kind": "forecast",
            "unit": VALUE_UNIT,
            "value": 0.25,
            "valid_from": "2024-05-05T14:00:00.000000Z",
        }
        base.update(overrides)
        return base

    def test_builds_sorted_nodes(self) -> None:
        records = [
            self._record(record_id="b", valid_from="2024-05-05T15:00:00Z", value=0.2),
            self._record(record_id="a", valid_from="2024-05-05T14:00:00Z", value=0.1),
        ]
        nodes = background_nodes_from_records(records)
        assert [n.record_id for n in nodes] == ["a", "b"]

    def test_skips_null_value_without_fabricating_zero(self) -> None:
        records = [self._record(value=None)]
        assert background_nodes_from_records(records) == []

    def test_rejects_wrong_source_id(self) -> None:
        with pytest.raises(UnsupportedMmodBackgroundRecordError):
            background_nodes_from_records([self._record(source_id="something-else")])

    def test_rejects_wrong_record_kind(self) -> None:
        with pytest.raises(UnsupportedMmodBackgroundRecordError):
            background_nodes_from_records([self._record(record_kind="observation")])

    def test_rejects_wrong_unit(self) -> None:
        with pytest.raises(UnsupportedMmodBackgroundRecordError):
            background_nodes_from_records([self._record(unit="percent")])


class TestAssessMmodBackground:
    def test_no_nodes_is_missing_data(self) -> None:
        assessment = assess_mmod_background(
            [],
            window_start=datetime(2024, 5, 5, 13, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 15, tzinfo=UTC),
        )
        assert assessment.status == "missing_data"
        assert assessment.max_level is None
        assert assessment.exceedance_hours_by_level is None
        assert assessment.critical_gap is True
        assert assessment.coverage_fraction == 0.0

    def test_fully_covered_background_window(self) -> None:
        nodes = [_node(0, 0.0), _node(1, 0.0), _node(2, 0.0)]
        assessment = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 5, 5, 0, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 2, tzinfo=UTC),
        )
        assert assessment.status == "ok"
        assert assessment.max_level == "background"
        assert assessment.exceedance_hours_by_level == {"elevated": 0.0, "pronounced": 0.0}
        assert assessment.critical_gap is False
        assert assessment.coverage_fraction == 1.0

    def test_fully_elevated_window(self) -> None:
        # ratio=1+0.5=1.5 на всём отрезке -> elevated все 2 часа, pronounced 0.
        nodes = [_node(0, 0.5), _node(1, 0.5), _node(2, 0.5)]
        assessment = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 5, 5, 0, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 2, tzinfo=UTC),
        )
        assert assessment.status == "ok"
        assert assessment.max_level == "elevated"
        assert assessment.exceedance_hours_by_level == pytest.approx(
            {"elevated": 2.0, "pronounced": 0.0}
        )

    def test_pronounced_implies_elevated_nested_exceedance(self) -> None:
        # ratio=1+1.5=2.5 -> выше обоих порогов весь час: elevated=1, pronounced=1.
        nodes = [_node(0, 1.5), _node(1, 1.5)]
        assessment = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 5, 5, 0, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 1, tzinfo=UTC),
        )
        assert assessment.max_level == "pronounced"
        exceedance = assessment.exceedance_hours_by_level
        assert exceedance is not None
        assert exceedance == pytest.approx({"elevated": 1.0, "pronounced": 1.0})
        assert exceedance["pronounced"] <= exceedance["elevated"]

    def test_linear_interpolation_crossing_threshold_is_analytically_exact(self) -> None:
        """factor линейно растёт с 0.0 (ratio=1.0) в 00:00 до 0.4 (ratio=1.4)
        в 02:00 — ratio=1.2 (порог elevated) пересекается ровно в середине
        (01:00), то есть elevated ровно 1 час из 2 (main-prompt.md §9
        «независимый пример»: аналитическое решение линейного уравнения,
        не повтор реализации)."""
        nodes = [_node(0, 0.0), _node(2, 0.4)]
        assessment = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 5, 5, 0, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 2, tzinfo=UTC),
        )
        assert assessment.status == "ok"
        exceedance = assessment.exceedance_hours_by_level
        assert exceedance is not None
        assert exceedance["elevated"] == pytest.approx(1.0, abs=1e-6)
        assert exceedance["pronounced"] == pytest.approx(0.0)

    def test_window_partially_outside_grid_is_missing_data_not_partial_credit(self) -> None:
        """Контракт (result.schema.json) требует max_level=None при
        status != 'ok' — частичное покрытие НЕ выдаёт частично посчитанные
        числа (main-prompt.md §2), даже если бо́льшая часть окна покрыта."""
        nodes = [_node(0, 0.5), _node(1, 0.5)]
        assessment = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 5, 5, 0, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 3, tzinfo=UTC),  # выходит за последний узел (01:00)
        )
        assert assessment.status == "missing_data"
        assert assessment.max_level is None
        assert assessment.exceedance_hours_by_level is None
        assert assessment.critical_gap is True
        assert 0.0 < assessment.coverage_fraction < 1.0

    def test_window_entirely_before_grid_start(self) -> None:
        nodes = [_node(0, 0.5), _node(1, 0.5)]
        assessment = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 5, 4, 20, tzinfo=UTC),
            window_end=datetime(2024, 5, 4, 22, tzinfo=UTC),
        )
        assert assessment.status == "missing_data"
        assert assessment.coverage_fraction == 0.0

    def test_document_grid_bounds_used_for_notes_even_with_partial_nodes(self) -> None:
        """Вызывающая сторона (src/sources/mmod.py) может передать только
        подмножество узлов года — document_grid_start/end остаются точными
        границами ВСЕГО документа, не только загруженного подмножества."""
        nodes = [_node(0, 0.5), _node(1, 0.5)]
        doc_start = datetime(2024, 1, 1, tzinfo=UTC)
        doc_end = datetime(2025, 1, 1, 6, 0, tzinfo=UTC)
        assessment = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 5, 5, 0, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 1, tzinfo=UTC),
            document_grid_start=doc_start,
            document_grid_end=doc_end,
        )
        assert assessment.status == "ok"
        assert any("2024-01-01" in note for note in assessment.notes)

    def test_record_ids_reference_only_nodes_actually_used(self) -> None:
        nodes = [
            _node(0, 0.0, record_id="rec-a"),
            _node(1, 0.0, record_id="rec-b"),
            _node(5, 0.0, record_id="rec-far-away"),
        ]
        assessment = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 5, 5, 0, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 1, tzinfo=UTC),
        )
        assert set(assessment.record_ids) == {"rec-a", "rec-b"}

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            assess_mmod_background(
                [], window_start=datetime(2024, 5, 5), window_end=datetime(2024, 5, 5, 1)
            )

    def test_window_end_before_start_rejected(self) -> None:
        with pytest.raises(ValueError, match="after"):
            assess_mmod_background(
                [],
                window_start=datetime(2024, 5, 5, 1, tzinfo=UTC),
                window_end=datetime(2024, 5, 5, tzinfo=UTC),
            )
