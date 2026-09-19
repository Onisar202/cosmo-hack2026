"""HTML-выгрузка из сохранённого результата (FN-36, S2-06, приёмка п.2/3/4/6)."""

from __future__ import annotations

from html import escape
from typing import Any

import pytest

from src.export import ExportError, export_result_html


def test_html_contains_key_fields_of_the_stored_result(
    contract_fixture: dict[str, Any],
) -> None:
    """Приёмка п.2: HTML содержит те же параметры, оценки, версии и
    ограничения — сравнивает ключевые поля HTML с сохранённым объектом.

    Текстовые поля сравниваются в HTML-экранированном виде: символы вроде
    ``>``/``<``/``&`` в свободном тексте результата (например ``>10 МэВ`` в
    предупреждении) обязаны быть экранированы в валидном HTML — сравнение с
    сырой строкой было бы неверным тестом, а не поблажкой для экспорта.
    """
    result = contract_fixture
    html = export_result_html(result)

    from src.export.html_export import _MODE_LABELS  # noqa: PLC0415

    assert result["result_id"] in html
    assert result["algorithm_version"] in html
    assert _MODE_LABELS[result["mode"]] in html
    assert result["request"]["start_at"][:10] in html  # хотя бы дата видна

    for window in result["windows"]:
        assert window["window_id"] in html
        for mechanism in window["mechanisms"]:
            if mechanism["max_level"] is not None:
                assert mechanism["max_level"] in html
            for note in mechanism["notes"]:
                assert escape(note) in html
            for record_id in mechanism["record_ids"]:
                assert record_id in html

    for entry in result["data_manifest"]:
        assert entry["record_id"] in html
        assert entry["source_id"] in html
        assert entry["source_version"] in html

    for status in result["source_status"]:
        assert status["source_id"] in html

    for limitation in result["limitations"]:
        assert escape(limitation) in html

    for warning in result["warnings"]:
        assert warning["code"] in html
        assert escape(warning["message"]) in html

    assert escape(result["recommendation"]["explanation"]) in html
    if result["recommendation"]["window_id"] is not None:
        assert result["recommendation"]["window_id"] in html


def test_html_shows_orbit_source_epoch_age_and_reconstruction_flag(
    success_result: dict[str, Any],
) -> None:
    from src.export.html_export import _ORBIT_SOURCE_LABELS  # noqa: PLC0415

    html = export_result_html(success_result)
    orbit = success_result["orbit"]

    assert _ORBIT_SOURCE_LABELS[orbit["source"]] in html
    assert orbit["norad_id"] in html
    assert orbit["record_id"] in html
    assert str(orbit["elements_age_hours"]) in html


def test_html_every_null_field_carries_an_explicit_reason(
    success_result: dict[str, Any],
) -> None:
    """Приёмка п.6: null имеет причину — не голый прочерк/пустая ячейка.

    ``success.json`` уже несёт null-поля (например ``windows[].lighting.note``
    отсутствует, ``source_status[*].last_error_at`` нет ни у одного источника
    — все успешны) — каждое такое место рендерится через ``_null_span`` с
    заголовком-причиной, никогда голой пустой ячейкой.
    """
    html = export_result_html(success_result)
    assert 'class="null-value"' in html
    # У каждого null-значения есть непустой title-атрибут (причина), а не
    # просто класс без объяснения.
    assert 'title=""' not in html


def test_html_static_labels_never_use_the_forbidden_safety_wording() -> None:
    """main-prompt.md §4/§12: эвристический уровень не называется «безопасно» —
    интерфейс/выгрузка не добавляют от себя формулировок об исходе, только
    подписи для фиксированных кодов контракта (labels.ts придерживается того
    же правила на фронтенде). Проверяет ровно то, что экспорт добавляет сам
    от себя (статические словари подписей и HTML-шаблон) — не свободный
    текст результата, который экспорту не подконтролен.
    """
    import src.export.html_export as m  # noqa: PLC0415

    label_dicts: list[dict[str, str]] = [
        m._MODE_LABELS,
        m._MECHANISM_LABELS,
        m._MECHANISM_STATUS_LABELS,
        m._MECHANISM_STATUS_NULL_REASONS,
        m._WARNING_SEVERITY_LABELS,
        m._LIGHTING_STATUS_LABELS,
        m._RECOMMENDATION_STATUS_LABELS,
        m._RECORD_KIND_LABELS,
        m._ORBIT_SOURCE_LABELS,
    ]
    for labels in label_dicts:
        for value in labels.values():
            assert "безопасн" not in value.lower()

    for static_text in (
        m._MECHANISM_UNITS_NOTE,
        m._STYLE,
        m._DEFAULT_NULL_REASON,
        m._NO_ERROR_LOGGED_REASON,
    ):
        assert "безопасн" not in static_text.lower()


def test_repeated_export_is_byte_for_byte_deterministic(success_result: dict[str, Any]) -> None:
    """Приёмка п.4: повторный экспорт детерминирован."""
    first = export_result_html(success_result)
    second = export_result_html(success_result)

    assert first == second


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda r: r.pop("orbit"), id="missing-required-field"),
        pytest.param(
            lambda r: r.update(computed_at="2026-09-18 12:00:00"),
            id="naive-datetime-no-offset",
        ),
    ],
)
def test_html_rejects_a_corrupted_payload_instead_of_a_plausible_report(
    success_result: dict[str, Any], corrupt: Any
) -> None:
    """Приёмка п.3: повреждённый/невалидный payload не даёт правдоподобный отчёт."""
    corrupt(success_result)

    with pytest.raises(ExportError):
        export_result_html(success_result)
