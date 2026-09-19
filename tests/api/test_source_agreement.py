"""Согласие источников ВНУТРИ механизма: ``source_agreement`` (FN-41).

Второй комментарий эксперта к FN-41: production-результат обязан отличать
конфликт ИСТОЧНИКОВ внутри одного механизма от конфликта МЕХАНИЗМОВ между
собой; обе оценки источников и их доказательства сохраняются, ни одна не
выбирается молча; ``SOURCE_CONFLICT`` понижает доверие и выводит окно на
проверку человеком, тогда как расхождение «космопогода против MMOD»
по-прежнему разбирается правилом предпочтения окон
(``src/domain/windows/dominance.py``, никакого нового ранжирования).

Проверяется и чистое правило (``_source_agreement``/``_combined_event_state``),
и его сквозной путь через production-сервис и контракт результата.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

from src.api.service import (
    _combined_event_state,
    _historical_event_warning,
    _historical_space_weather_mechanism,
    _source_agreement,
    _validate_result_or_raise,
)
from src.domain.spaceweather.archive_assessment import (
    ArchiveEvent,
    ArchiveWindowAssessment,
)
from tests.api.conftest import wait_for_job

UTC = timezone.utc
WINDOW_START = datetime(2024, 6, 20, 9, 0, tzinfo=UTC)
WINDOW_END = datetime(2024, 6, 20, 13, 0, tzinfo=UTC)
AS_OF = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)


def _assessment(
    source_id: str,
    status: str,
    *,
    unresolved: tuple[ArchiveEvent, ...] = (),
) -> ArchiveWindowAssessment:
    return ArchiveWindowAssessment(
        status=status,  # type: ignore[arg-type]
        source_id=source_id,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        as_of=AS_OF,
        horizon_end=None,
        beyond_horizon=False,
        critical_gap=status != "NO_EVENT_DETECTED",
        coverage_fraction=1.0,
        events=(),
        unresolved_open_events=unresolved,
        record_ids=(f"rec-{source_id}",),
        notes=(f"линия {source_id}: {status}",),
    )


def test_two_agreeing_lines_are_consistent() -> None:
    assessments = [
        _assessment("line-a", "NO_EVENT_DETECTED"),
        _assessment("line-b", "NO_EVENT_DETECTED"),
    ]
    assert _source_agreement(assessments) == "CONSISTENT"
    assert _combined_event_state(assessments) == "NO_EVENT_DETECTED"


def test_two_disagreeing_lines_are_a_conflict_and_never_silently_resolved() -> None:
    assessments = [
        _assessment("line-a", "EVENT_PRESENT"),
        _assessment("line-b", "NO_EVENT_DETECTED"),
    ]
    assert _source_agreement(assessments) == "CONFLICT"
    # Консервативно в сторону тревоги: подтверждённое событие не отменяется
    # тем, что вторая линия его не увидела.
    assert _combined_event_state(assessments) == "EVENT_PRESENT"

    mechanism = _historical_space_weather_mechanism(
        assessments,
        status="qualitative_only",
        agreement="CONFLICT",
        event_state="EVENT_PRESENT",
    )
    # Обе оценки сохранены рядом — ни одна не выбрана молча.
    assert [line["source_id"] for line in mechanism["source_assessments"]] == [
        "line-a",
        "line-b",
    ]
    assert [line["event_state"] for line in mechanism["source_assessments"]] == [
        "EVENT_PRESENT",
        "NO_EVENT_DETECTED",
    ]
    # Конфликт источников выводит окно на проверку человеком через уже
    # существующий механизм исключения из сравнения — без нового ранжирования.
    assert mechanism["critical_gap"] is True
    assert any("КОНФЛИКТ ИСТОЧНИКОВ" in note for note in mechanism["notes"])

    warning = _historical_event_warning(
        mechanism, assessments, window_id="win-a", attempt_id="fa-test"
    )
    assert warning is not None
    assert warning["code"] == "space-weather-source-conflict"
    assert warning["severity"] == "critical"


def test_a_single_determinate_line_cannot_establish_agreement() -> None:
    """Третье состояние, а не «источники согласны»: одна линия сама с собой
    не согласуется (main-prompt.md §2)."""
    assessments = [
        _assessment("line-a", "NO_EVENT_DETECTED"),
        _assessment("line-b", "INSUFFICIENT_DATA"),
    ]
    assert _source_agreement(assessments) == "INSUFFICIENT_DATA"
    # Но доказательство, полученное умеющей его дать линией, не теряется.
    assert _combined_event_state(assessments) == "NO_EVENT_DETECTED"
    assert _source_agreement([_assessment("line-a", "NO_EVENT_DETECTED")]) == (
        "INSUFFICIENT_DATA"
    )
    assert _source_agreement([]) == "INSUFFICIENT_DATA"


def test_conflicting_mechanism_satisfies_the_result_contract(
    app_client: TestClient,
) -> None:
    """Контракт (``contracts/result.schema.json``) обязан принимать конфликт
    источников и обязан ОТКЛОНЯТЬ конфликт, не выведенный из сравнения:
    иначе окно с расхождением линий попало бы в автоматическую рекомендацию.
    """
    create = app_client.post(
        "/api/calculations",
        json={
            "mode": "historical_analysis",
            "start_at": "2024-06-20T09:00:00Z",
            "duration_hours": 4,
            "search_window_hours": 8,
        },
    )
    job = wait_for_job(app_client, create.json()["task_id"])
    assert job["status"] == "done", job
    stored: dict[str, Any] = app_client.get(f"/api/results/{job['result_id']}").json()

    conflicting = _historical_space_weather_mechanism(
        [
            _assessment("nasa-donki-notifications", "EVENT_PRESENT"),
            _assessment("noaa-swpc-forecast-discussion-archive", "NO_EVENT_DETECTED"),
        ],
        status="qualitative_only",
        agreement="CONFLICT",
        event_state="EVENT_PRESENT",
    )
    with_conflict = copy.deepcopy(stored)
    with_conflict["windows"][0]["mechanisms"][0] = conflicting
    with_conflict["windows"][0]["excluded_from_comparison"] = True
    with_conflict["windows"][0]["exclusion_reason"] = "конфликт источников механизма 1"
    _validate_result_or_raise(with_conflict)  # не должен поднять исключение

    # А вот конфликт БЕЗ вывода окна из сравнения контракт не пропускает.
    smuggled = copy.deepcopy(with_conflict)
    smuggled["windows"][0]["mechanisms"][0]["critical_gap"] = False
    try:
        _validate_result_or_raise(smuggled)
    except Exception as exc:  # noqa: BLE001 — важен сам факт отказа контракта
        assert "critical_gap" in str(exc)
    else:  # pragma: no cover - страховка от молчаливого ослабления контракта
        raise AssertionError(
            "контракт обязан отклонять source_agreement=CONFLICT без critical_gap"
        )


def test_every_configured_archive_line_is_visible_in_the_saved_result(
    app_client: TestClient,
) -> None:
    """Ни одна настроенная линия механизма не исчезает из результата, даже
    если ей нечего сказать: именно это отличает «согласие установить нельзя»
    от «источники согласны» (второй комментарий Jira FN-41)."""
    create = app_client.post(
        "/api/calculations",
        json={
            "mode": "historical_forecast",
            "start_at": "2024-06-20T09:00:00Z",
            "duration_hours": 4,
            "search_window_hours": 8,
            "as_of": "2024-06-20T00:00:00Z",
        },
    )
    job = wait_for_job(app_client, create.json()["task_id"])
    assert job["status"] == "done", job
    stored = app_client.get(f"/api/results/{job['result_id']}").json()

    for window in stored["windows"]:
        space_weather = next(
            m for m in window["mechanisms"] if m["mechanism"] == "space_weather"
        )
        assert space_weather["source_agreement"] in {
            "CONSISTENT",
            "CONFLICT",
            "INSUFFICIENT_DATA",
        }
        seen = {line["source_id"] for line in space_weather["source_assessments"]}
        assert seen == {
            "nasa-donki-notifications",
            "noaa-swpc-forecast-discussion-archive",
        }
        for line in space_weather["source_assessments"]:
            assert line["notes"], "оценка линии обязана объяснять себя (критерий О4)"

    # Выгрузки читают тот же объект и несут то же поле.
    exported = app_client.get(f"/api/results/{job['result_id']}/export.json").json()
    assert exported == stored
    html = app_client.get(f"/api/results/{job['result_id']}/export.html").text
    assert "Согласие источников" in html
