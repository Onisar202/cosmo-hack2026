"""Тесты архивной событийной линии Механизма 1 (FN-41, этап 3).

Чистый расчётный модуль — ни сети, ни хранилища, ни системного «сейчас»
(.ai/main-prompt.md §8, §9): все входы задаются явно. Каждый тест закрывает
конкретный способ получить правдоподобный, но неверный результат — прежде
всего тот, ради которого три состояния и введены: молчание архива, выданное
за «событий не было».
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.spaceweather.historical_events import (
    ArchivedNotification,
    UnsupportedRecordError,
    archived_notifications_from_records,
    assess_archived_events,
)

UTC = timezone.utc
QUALIFYING = ("SEP", "GST")
LOOKBACK = 24.0

WINDOW_START = datetime(2024, 6, 20, 9, 0, tzinfo=UTC)
WINDOW_END = datetime(2024, 6, 20, 15, 0, tzinfo=UTC)
#: Покрытие с запасом на обратный интервал (окно минус 24 ч) — именно его
#: подтверждает успешный шлюз получения.
FULL_COVERAGE = [(datetime(2024, 6, 19, 0, 0, tzinfo=UTC), datetime(2024, 6, 21, 0, 0, tzinfo=UTC))]


def _notification(
    *,
    record_id: str = "rec-1",
    message_type: str = "SEP",
    event_start: datetime,
    event_end: datetime | None = None,
    published_at: datetime | None = None,
) -> ArchivedNotification:
    return ArchivedNotification(
        record_id=record_id,
        provider_record_id=f"provider-{record_id}",
        message_type=message_type,
        event_start=event_start,
        event_end=event_end if event_end is not None else event_start,
        published_at=published_at,
    )


def _assess(notifications, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "coverage_intervals": FULL_COVERAGE,
        "qualifying_message_types": QUALIFYING,
        "persistence_lookback_hours": LOOKBACK,
    }
    kwargs.update(overrides)
    return assess_archived_events(notifications, **kwargs)  # type: ignore[arg-type]


def test_confirmed_coverage_without_qualifying_notifications_is_no_event_detected() -> None:
    """Контрольный спокойный период (16–27 июня 2024, main-prompt.md §11):
    покрытие подтверждено, квалифицирующих уведомлений нет — это
    ПОДТВЕРЖДЁННОЕ отсутствие события, отличимое от «нет данных»."""
    assessment = _assess([])

    assert assessment.state == "NO_EVENT_DETECTED"
    assert assessment.critical_gap is False
    assert assessment.coverage_fraction == 1.0
    assert assessment.record_ids == ()


def test_unconfirmed_coverage_is_insufficient_data_not_no_event() -> None:
    """Главный запрет задачи: пустой архив без подтверждённого покрытия — не
    «событий не было» (main-prompt.md §2). Пустой список интервалов — это
    ровно то, что отдаёт шлюз при отказе получения."""
    assessment = _assess([], coverage_intervals=[])

    assert assessment.state == "INSUFFICIENT_DATA"
    assert assessment.critical_gap is True
    assert assessment.coverage_fraction == 0.0


def test_partial_coverage_is_insufficient_data() -> None:
    """Частичное покрытие не выдаётся за полное: интервал обрывается за два
    часа до конца окна."""
    assessment = _assess(
        [],
        coverage_intervals=[
            (datetime(2024, 6, 19, 0, 0, tzinfo=UTC), datetime(2024, 6, 20, 13, 0, tzinfo=UTC))
        ],
    )

    assert assessment.state == "INSUFFICIENT_DATA"
    assert 0.0 < assessment.coverage_fraction < 1.0


def test_qualifying_notification_inside_the_window_is_event_present() -> None:
    assessment = _assess(
        [_notification(event_start=datetime(2024, 6, 20, 10, 0, tzinfo=UTC))]
    )

    assert assessment.state == "EVENT_PRESENT"
    assert assessment.critical_gap is True
    assert assessment.event_count == 1
    assert assessment.record_ids == ("rec-1",)


def test_event_announced_shortly_before_the_window_still_counts() -> None:
    """DONKI не выпускает сообщения об окончании события: окно, начавшееся
    через час после уведомления, нельзя считать спокойным (обратный запас,
    модульный docstring)."""
    assessment = _assess(
        [_notification(event_start=WINDOW_START - timedelta(hours=1))]
    )

    assert assessment.state == "EVENT_PRESENT"


def test_event_older_than_the_lookback_does_not_count() -> None:
    """Запас конечен и задан конфигурацией: событие двухдневной давности не
    делает окно «событийным» бессрочно."""
    assessment = _assess(
        [_notification(event_start=WINDOW_START - timedelta(hours=LOOKBACK + 1))]
    )

    assert assessment.state == "NO_EVENT_DETECTED"


def test_non_qualifying_message_types_do_not_create_an_event() -> None:
    """Вспышка/CME/IPS — признаки того же явления на другом этапе, а не
    отдельное воздействие на окно (main-prompt.md §4, sources.yaml →
    qualifying_message_types)."""
    assessment = _assess(
        [
            _notification(
                record_id="rec-flr",
                message_type="FLR",
                event_start=datetime(2024, 6, 20, 10, 0, tzinfo=UTC),
            )
        ]
    )

    assert assessment.state == "NO_EVENT_DETECTED"
    assert assessment.record_ids == ()


def test_related_notifications_of_one_activity_give_one_contribution() -> None:
    """Реальный случай ``20240510-AL-013``/``AL-014`` — два уведомления об
    одной активности ``2024-05-10T15:00:00-GST-001`` (main-prompt.md §4
    «связанные сигналы одного события дают один вклад»)."""
    moment = datetime(2024, 6, 20, 11, 0, tzinfo=UTC)
    assessment = _assess(
        [
            _notification(record_id="rec-a", message_type="GST", event_start=moment),
            _notification(record_id="rec-b", message_type="GST", event_start=moment),
        ]
    )

    assert assessment.state == "EVENT_PRESENT"
    assert assessment.event_count == 1  # не два
    assert assessment.record_ids == ("rec-a", "rec-b")  # прослеживаемость к обоим


def test_ambiguous_publication_time_blocks_the_no_event_conclusion() -> None:
    """Уведомление с неразрешимым временем публикации невидимо строгой
    выборке — значит подтвердить отсутствие события нечем (main-prompt.md §1)."""
    assessment = _assess([], ambiguous_publication_count=1)

    assert assessment.state == "INSUFFICIENT_DATA"
    assert assessment.critical_gap is True


def test_confirmed_event_outranks_an_ambiguous_publication() -> None:
    """Знание о событии определённо: неразрешимое время публикации ДРУГОГО
    уведомления его не отменяет."""
    assessment = _assess(
        [_notification(event_start=datetime(2024, 6, 20, 10, 0, tzinfo=UTC))],
        ambiguous_publication_count=3,
    )

    assert assessment.state == "EVENT_PRESENT"


def test_notes_never_claim_quiet_conditions_when_data_is_missing() -> None:
    assessment = _assess([], coverage_intervals=[])
    joined = " ".join(assessment.notes)

    assert "подтверждено" in joined  # объяснение, чего именно не хватает
    assert "main-prompt.md §2" in joined


def test_records_from_another_source_are_rejected_not_silently_ignored() -> None:
    with pytest.raises(UnsupportedRecordError):
        archived_notifications_from_records(
            [
                {
                    "record_id": "rec-x",
                    "provider_record_id": "p",
                    "source_id": "noaa-swpc-proton-flux",
                    "record_kind": "observation",
                    "valid_from": "2024-06-20T10:00:00Z",
                    "valid_to": "2024-06-20T10:00:00Z",
                    "published_at": None,
                    "spatial_context": {"message_type": "SEP"},
                }
            ]
        )


def test_records_are_read_by_event_time_not_publication_time() -> None:
    """``valid_from``/``valid_to`` записи — время СОБЫТИЯ (извлечено
    источником из ``Activity ID``), ``published_at`` — время выпуска; подмена
    одного другим — главная ловушка main-prompt.md §1."""
    notifications = archived_notifications_from_records(
        [
            {
                "record_id": "rec-1",
                "provider_record_id": "20240620-AL-001",
                "source_id": "nasa-donki-notifications",
                "record_kind": "warning",
                "valid_from": "2024-06-20T10:00:00Z",
                "valid_to": "2024-06-20T10:00:00Z",
                "published_at": "2024-06-20T10:14:03Z",
                "spatial_context": {"message_type": "SEP"},
            }
        ]
    )

    assert notifications[0].event_start == datetime(2024, 6, 20, 10, 0, tzinfo=UTC)
    assert notifications[0].published_at == datetime(2024, 6, 20, 10, 14, 3, tzinfo=UTC)
