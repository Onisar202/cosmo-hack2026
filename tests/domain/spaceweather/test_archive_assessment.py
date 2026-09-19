"""Три состояния архивной оценки окна ВКД (FN-42).

Чистые доменные тесты: без сети и без хранилища (main-prompt.md §8). Записи
собираются из **реальных** сохранённых архивных ответов
(``tests/fixtures/sources/archive/``) теми же парсерами, что и в production
(``src/sources/archive_probe.py``), и приводятся к форме
``contracts/record.schema.json`` локальным помощником — доменный слой видит
ровно ту форму, которую отдаёт ``select_as_of``.

Совпадение этой формы с настоящей формой хранилища закреплено отдельно:
``tests/sources/test_archive_ingest.py`` прогоняет тот же доменный расчёт на
записях, реально прошедших через ``insert_record``/``select_as_of``, — если
формы разойдутся, эти тесты разойдутся вместе с ними.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.domain.spaceweather.archive_assessment import (
    ArchiveProductPolicy,
    CoverageInterval,
    TemporalLeakError,
    UnsupportedRecordError,
    assess_archive_window,
)
from src.sources.archive_probe import (
    donki_notification_to_record_input,
    parse_donki_notifications,
)
from src.store.records import RecordInput

UTC = timezone.utc
ARCHIVE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "fixtures" / "sources" / "archive"
)
DONKI_SOURCE_ID = "nasa-donki-notifications"
FETCHED_AT = datetime(2026, 9, 18, 23, 3, 31, tzinfo=UTC)

#: Политика с установленным горизонтом — ЗНАЧЕНИЕ ЗАДАЁТСЯ ТЕСТОМ, чтобы
#: показать работу механизма. В ``sources.yaml`` горизонт остаётся ``null``
#: до ответа кейсодержателя (приёмка FN-42 п.5).
DONKI_POLICY_6H = ArchiveProductPolicy(
    source_id=DONKI_SOURCE_ID,
    forecast_horizon_hours=6.0,
    event_message_types=frozenset({"SEP"}),
)
#: Политика в том виде, в каком реестр описывает продукт сегодня.
DONKI_POLICY_UNKNOWN_HORIZON = ArchiveProductPolicy(
    source_id=DONKI_SOURCE_ID,
    forecast_horizon_hours=None,
    event_message_types=frozenset({"SEP"}),
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _payload(record_input: RecordInput, record_id: str) -> dict[str, Any]:
    """Форма ``contracts/record.schema.json`` для полей, которые читает домен."""
    return {
        "record_id": record_id,
        "provider_record_id": record_input.provider_record_id,
        "source_id": record_input.source_id,
        "record_kind": record_input.record_kind,
        "observed_at": _iso(record_input.observed_at),
        "valid_from": _iso(record_input.valid_from),
        "valid_to": _iso(record_input.valid_to),
        "published_at": (
            _iso(record_input.published_at) if record_input.published_at else None
        ),
        "spatial_context": record_input.spatial_context,
        "source_version": record_input.source_version,
    }


def _real_donki_records(filename: str) -> list[dict[str, Any]]:
    """Реальные уведомления файла, нормализованные и приведённые к форме записи."""
    raw = (ARCHIVE_DIR / filename).read_bytes()
    payloads: list[dict[str, Any]] = []
    for index, notification in enumerate(parse_donki_notifications(raw)):
        record_input = donki_notification_to_record_input(
            notification,
            source_url="https://api.nasa.gov/DONKI/notifications",
            fetched_at=FETCHED_AT,
        )
        if record_input is None:
            continue
        payloads.append(_payload(record_input, f"rec-{index}"))
    return payloads


def _eligible(records: list[dict[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
    """То же правило, что и у ``select_as_of``: published_at известен и <= as_of.

    Повторяется здесь только потому, что эти тесты сознательно обходятся без
    хранилища; само правило проверяется на настоящем SQL в
    ``tests/store/test_as_of.py`` и ``tests/sources/test_archive_ingest.py``.
    """
    result = []
    for record in records:
        published = record["published_at"]
        if published is None:
            continue
        if datetime.fromisoformat(published.replace("Z", "+00:00")) <= as_of:
            result.append(record)
    return result


MAY_INTERVAL = CoverageInterval(
    start=datetime(2024, 5, 1, tzinfo=UTC),
    end=datetime(2024, 5, 16, tzinfo=UTC),
    fetched_at=FETCHED_AT,
)
JUNE_INTERVAL = CoverageInterval(
    start=datetime(2024, 6, 16, tzinfo=UTC),
    end=datetime(2024, 7, 1, tzinfo=UTC),
    fetched_at=FETCHED_AT,
)


# ---------------------------------------------------------------------------
# EVENT_PRESENT
# ---------------------------------------------------------------------------


def test_real_proton_event_gives_event_present() -> None:
    """Реальное протонное событие 10 мая 2024: ``20240510-AL-004`` (13:46Z)
    о активности ``2024-05-10T13:35:00-SEP-001``."""
    as_of = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-05-01_2024-05-15.json"), as_of)

    assessment = assess_archive_window(
        records,
        policy=DONKI_POLICY_6H,
        ingested_intervals=[MAY_INTERVAL],
        window_start=datetime(2024, 5, 10, 13, 0, tzinfo=UTC),
        window_end=as_of,
        as_of=as_of,
    )

    assert assessment.status == "EVENT_PRESENT"
    assert "20240510-AL-004" in {e.provider_record_id for e in assessment.events}


def test_window_after_a_continuing_sep_onset_is_insufficient_not_calm() -> None:
    """Round 3 ревью PR #37: реальный SEP ``2024-05-10T13:35:00-SEP-001``
    (``20240510-AL-004``, выпущено 13:46Z) сообщает только НАЧАЛО явления —
    DONKI не публикует структурированного момента его завершения. Окно,
    целиком лежащее ПОСЛЕ 13:46Z в тот же день, не может получить
    ``NO_EVENT_DETECTED`` только из-за отсутствия точного пересечения с
    точкой начала: продолжалось ли явление на 16:00Z — неизвестно."""
    as_of = datetime(2024, 5, 10, 18, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-05-01_2024-05-15.json"), as_of)

    assessment = assess_archive_window(
        records,
        policy=DONKI_POLICY_6H,
        ingested_intervals=[MAY_INTERVAL],
        window_start=datetime(2024, 5, 10, 16, 0, tzinfo=UTC),
        window_end=datetime(2024, 5, 10, 17, 0, tzinfo=UTC),
        as_of=as_of,
    )

    assert assessment.events == ()
    assert assessment.status == "INSUFFICIENT_DATA"
    assert "20240510-AL-004" in {
        e.provider_record_id for e in assessment.unresolved_open_events
    }
    assert any("без задокументированного конца" in note for note in assessment.notes)


def test_only_configured_event_types_create_an_event() -> None:
    """Связанные сигналы того же явления (CME/FLR/GST/IPS/MPC/RBE) не создают
    собственного события Механизма 1 — перечень типов приходит из
    конфигурации, а не из условия в домене (main-prompt.md §4, §11:
    геомагнитная активность — модулятор, а не слагаемое)."""
    as_of = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-05-01_2024-05-15.json"), as_of)

    def assess(event_types: frozenset[str]) -> Any:
        return assess_archive_window(
            records,
            policy=ArchiveProductPolicy(
                source_id=DONKI_SOURCE_ID,
                forecast_horizon_hours=6.0,
                event_message_types=event_types,
            ),
            ingested_intervals=[MAY_INTERVAL],
            window_start=datetime(2024, 5, 10, 13, 0, tzinfo=UTC),
            window_end=as_of,
            as_of=as_of,
        )

    # Один и тот же набор реальных записей, разная конфигурация — разный
    # результат. Это и есть доказательство, что перечень событийных типов
    # является данными, а не условием, зашитым в расчёт.
    with_sep = assess(frozenset({"SEP"}))
    assert with_sep.status == "EVENT_PRESENT"
    assert {e.message_type for e in with_sep.events} == {"SEP"}

    # GST тоже извлекается из Activity ID — точка начала без
    # задокументированного конца (round 3 ревью PR #37). В этом же архиве
    # есть более ранняя, ничем не подтверждённая как завершившаяся
    # геомагнитная буря (20240502-AL-004/005/006, начало 2024-05-02T15:00Z) —
    # окно 10 мая при конфигурации GST поэтому получает ``INSUFFICIENT_DATA``,
    # а не ``NO_EVENT_DETECTED``: это тот же самый механизм открытого конца,
    # что и у SEP, применённый к другому типу через ту же конфигурацию, не
    # через условие, зашитое под конкретный тип в домене.
    only_gst = assess(frozenset({"GST"}))
    assert only_gst.events == ()
    assert only_gst.status == "INSUFFICIENT_DATA"
    assert {e.provider_record_id for e in only_gst.unresolved_open_events} >= {
        "20240502-AL-004"
    }


# ---------------------------------------------------------------------------
# NO_EVENT_DETECTED — доказанное отсутствие события
# ---------------------------------------------------------------------------


def test_control_quiet_period_gives_no_event_detected() -> None:
    """Контрольный период 16–27 июня 2024 (main-prompt.md §11): окно целиком
    внутри прочитанного интервала архива, уведомлений SEP нет."""
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-06-16_2024-06-30.json"), as_of)

    assessment = assess_archive_window(
        records,
        policy=DONKI_POLICY_6H,
        ingested_intervals=[JUNE_INTERVAL],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )

    assert assessment.status == "NO_EVENT_DETECTED"
    assert assessment.coverage_fraction == 1.0
    assert assessment.critical_gap is False


def test_no_event_detected_is_not_worded_as_confirmed_calm() -> None:
    """«Порог не пересечён в прочитанном интервале» — не «обстановка
    подтверждена спокойной» (docs/method.md §6 п.5)."""
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-06-16_2024-06-30.json"), as_of)
    assessment = assess_archive_window(
        records,
        policy=DONKI_POLICY_6H,
        ingested_intervals=[JUNE_INTERVAL],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )
    joined = " ".join(assessment.notes)
    assert "не «обстановка подтверждена спокойной»" in joined


# ---------------------------------------------------------------------------
# INSUFFICIENT_DATA — пробел, горизонт, неразбираемый продукт
# ---------------------------------------------------------------------------


def test_unknown_horizon_makes_a_forward_window_insufficient_not_calm() -> None:
    """Приёмка п.5: пока горизонт продукта не установлен, окно вперёд от
    отсечения — «не покрыто», а не «спокойно»."""
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-06-16_2024-06-30.json"), as_of)

    assessment = assess_archive_window(
        records,
        policy=DONKI_POLICY_UNKNOWN_HORIZON,
        ingested_intervals=[JUNE_INTERVAL],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )

    assert assessment.status == "INSUFFICIENT_DATA"
    assert assessment.beyond_horizon is True
    assert assessment.horizon_end is None
    assert assessment.coverage_fraction == 0.0


@pytest.mark.parametrize("hours", [3.0, 6.0, 24.0])
def test_horizon_end_follows_the_configured_value(hours: float) -> None:
    """Горизонт — значение конфигурации: ``horizon_end`` сдвигается ровно на
    заданное число часов. Ни 6, ни 24 не зашиты в расчёт (приёмка п.5)."""
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-06-16_2024-06-30.json"), as_of)
    policy = ArchiveProductPolicy(
        source_id=DONKI_SOURCE_ID,
        forecast_horizon_hours=hours,
        event_message_types=frozenset({"SEP"}),
    )
    assessment = assess_archive_window(
        records,
        policy=policy,
        ingested_intervals=[JUNE_INTERVAL],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )
    assert assessment.horizon_end == as_of + timedelta(hours=hours)


def test_partial_horizon_leaves_a_critical_gap() -> None:
    """Горизонт короче окна: покрыта только часть окна — критический пробел,
    а не благоприятная оценка на весь интервал."""
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-06-16_2024-06-30.json"), as_of)
    policy = ArchiveProductPolicy(
        source_id=DONKI_SOURCE_ID,
        forecast_horizon_hours=3.0,
        event_message_types=frozenset({"SEP"}),
    )
    assessment = assess_archive_window(
        records,
        policy=policy,
        ingested_intervals=[JUNE_INTERVAL],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )
    assert assessment.status == "INSUFFICIENT_DATA"
    assert assessment.coverage_fraction == pytest.approx(0.5)
    assert assessment.critical_gap is True
    assert assessment.beyond_horizon is True


def test_window_outside_any_ingested_interval_is_insufficient_data() -> None:
    """Интервал архива, который никто не читал, не может дать «событий нет»."""
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    records = _eligible(_real_donki_records("donki_2024-06-16_2024-06-30.json"), as_of)
    assessment = assess_archive_window(
        records,
        policy=DONKI_POLICY_6H,
        ingested_intervals=[],  # ничего не загружено
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )
    assert assessment.status == "INSUFFICIENT_DATA"
    assert assessment.coverage_fraction == 0.0


def test_product_without_machine_readable_events_can_never_prove_absence() -> None:
    """Продукт со свободным текстом (пустой ``event_message_types``, реальный
    случай NOAA SWPC Forecast Discussion) структурно не может дать
    ``NO_EVENT_DETECTED`` — только ``INSUFFICIENT_DATA``."""
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    prose_policy = ArchiveProductPolicy(
        source_id=DONKI_SOURCE_ID,
        forecast_horizon_hours=6.0,
        event_message_types=frozenset(),
    )
    assessment = assess_archive_window(
        [],
        policy=prose_policy,
        ingested_intervals=[JUNE_INTERVAL],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )
    assert assessment.status == "INSUFFICIENT_DATA"
    assert any("машиночитаемого признака события" in note for note in assessment.notes)


def test_insufficient_data_is_never_worded_as_calm() -> None:
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    assessment = assess_archive_window(
        [],
        policy=DONKI_POLICY_UNKNOWN_HORIZON,
        ingested_intervals=[],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )
    joined = " ".join(assessment.notes)
    assert "оценить невозможно" in joined
    assert "не благоприятная обстановка" in joined


# ---------------------------------------------------------------------------
# Временная честность — структурный предохранитель
# ---------------------------------------------------------------------------


def test_record_published_after_as_of_raises_instead_of_being_counted() -> None:
    """main-prompt.md §1: запись из будущего относительно отсечения не может
    быть ни учтена, ни тихо отброшена — это ошибка пайплайна."""
    as_of = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    all_records = _real_donki_records("donki_2024-05-01_2024-05-15.json")
    late = [
        r
        for r in all_records
        if r["published_at"] is not None
        and datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")) > as_of
    ]
    assert late, "the real archive must contain notifications published after this cutoff"

    with pytest.raises(TemporalLeakError):
        assess_archive_window(
            late[:1],
            policy=DONKI_POLICY_6H,
            ingested_intervals=[MAY_INTERVAL],
            window_start=datetime(2024, 5, 10, 13, 0, tzinfo=UTC),
            window_end=as_of,
            as_of=as_of,
        )


def test_record_without_publication_time_is_rejected() -> None:
    """Запись с ``published_at=null`` навсегда непригодна для строгого replay
    и не должна доходить до этой оценки (приёмка п.4)."""
    raw = (ARCHIVE_DIR / "donki_2024-05-16_2024-05-31.json").read_bytes()
    by_id = {n.message_id: n for n in parse_donki_notifications(raw)}
    record_input = donki_notification_to_record_input(
        by_id["20240516-7D-001"],
        source_url="https://api.nasa.gov/DONKI/notifications",
        fetched_at=FETCHED_AT,
    )
    assert record_input is not None and record_input.published_at is None

    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(UnsupportedRecordError):
        assess_archive_window(
            [_payload(record_input, "rec-x")],
            policy=DONKI_POLICY_6H,
            ingested_intervals=[MAY_INTERVAL],
            window_start=datetime(2024, 5, 10, tzinfo=UTC),
            window_end=datetime(2024, 5, 11, tzinfo=UTC),
            as_of=as_of,
        )


def test_record_of_another_product_is_rejected() -> None:
    as_of = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    foreign = {
        "record_id": "rec-foreign",
        "provider_record_id": "x",
        "source_id": "noaa-swpc-proton-flux",
        "valid_from": _iso(as_of),
        "valid_to": _iso(as_of),
        "published_at": _iso(as_of),
        "spatial_context": {},
    }
    with pytest.raises(UnsupportedRecordError):
        assess_archive_window(
            [foreign],
            policy=DONKI_POLICY_6H,
            ingested_intervals=[MAY_INTERVAL],
            window_start=datetime(2024, 5, 10, 13, 0, tzinfo=UTC),
            window_end=as_of,
            as_of=as_of,
        )


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError):
        assess_archive_window(
            [],
            policy=DONKI_POLICY_6H,
            ingested_intervals=[],
            window_start=datetime(2024, 5, 10, 13, 0),
            window_end=datetime(2024, 5, 10, 14, 0),
            as_of=datetime(2024, 5, 10, 14, 0),
        )


def test_json_fixture_files_are_real_saved_responses() -> None:
    """Страховка от подмены реальных фикстур синтетическими: у каждого
    использованного файла есть *.meta.json с URL, статусом и SHA-256 тела
    (README фикстур), и сумма сходится."""
    for name in ("donki_2024-05-01_2024-05-15.json", "donki_2024-06-16_2024-06-30.json"):
        meta = json.loads((ARCHIVE_DIR / f"{name.removesuffix('.json')}.meta.json").read_bytes())
        body = (ARCHIVE_DIR / name).read_bytes()
        assert meta["http_status"] == 200
        assert hashlib.sha256(body).hexdigest() == meta["body_sha256"]
