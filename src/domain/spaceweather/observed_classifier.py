"""Механизм 1: интерпретация НАБЛЮДАЕМОГО потока протонов GOES (FN-38, S2-08).

Дополняет ``src/domain/spaceweather/external_forecast.py`` (FN-31, S2-01,
внешний СУТОЧНЫЙ прогноз-вероятность NOAA 3-Day) второй, отдельной линией
Механизма 1: классификацию НАБЛЮДЕНИЯ (не прогноза) — интегрального потока
протонов ``>=10 MeV`` (``pfu``), уже получаемого и сохраняемого
``src/sources/swpc.py`` (FN-22) — по шкале S NOAA согласно таблице
``.ai/main-prompt.md`` §11 «Механизм 1. Радиационная обстановка»:

| Уровень | Порог, pfu |
| --- | --- |
| Фон | ниже 10 |
| S1  | 10 и выше |
| S2  | 100 и выше |
| S3  | 1000 и выше |

Пороги взяты из этой таблицы дословно — не подобраны и не откалиброваны
здесь (main-prompt.md §4 «пороги берутся из шкал и источников, а не
подбираются»; проверено `contracts/result.schema.json` →
``mechanismAssessment`` allOf: уровни ``exceedance_hours_by_level`` для
``space_weather`` — ровно ``{S1, S2, S3}``, те же три имени).

Чистый расчётный модуль (.ai/main-prompt.md §8): не ходит в сеть и не
обращается к хранилищу — принимает уже выбранные записи (форма
``contracts/record.schema.json``, как их отдаёт
``src.store.select_observed_range``) и текущий момент ``now``, ничего не
подставляет вместо реального ``now`` сам.

**Наблюдение — не прогноз (main-prompt.md §11 «Прогноз ... минимум на 6
часов»).** GOES pfu — мгновенный отсчёт текущей интенсивности потока, у него
нет собственного горизонта прогноза вперёд: этот модуль честно НЕ
экстраполирует последний отсчёт на будущее окно (это был бы ровно
«базовый метод для сравнения: сохранение последнего наблюдения на весь
горизонт», явно названный в main-prompt.md §11 наивным способом, с которым
сравнивается настоящая реализация, а не сама реализация). Часть окна ПОСЛЕ
``now`` — ``beyond_horizon`` («не покрыто», main-prompt.md §4), не
«спокойно». Обоснованный внешний прогноз минимум на 6 часов вперёд для
Механизма 1 даёт отдельная линия — NOAA 3-Day Forecast (FN-31,
``external_forecast.py``); её суточная вероятность не создаёт здесь уровня
или ``exceedance``, ровно как и до этой задачи (см. её собственный докстринг).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc

#: Единственный зарегистрированный источник этого наблюдения
#: (``sources.yaml`` → ``space_weather[0].id``, ``src/sources/swpc.py::SOURCE_ID``).
#: Строка продублирована буквально (не импортом из ``src.sources``), тем же
#: способом, что и ``_ACCEPTED_SOURCE_IDS`` в ``external_forecast.py`` —
#: domain/ не читает src/sources/ (main-prompt.md §8 «расчёт не ходит в
#: сеть»; тот же модуль не обязан знать о шлюзе получения, только о форме
#: уже полученной записи).
_ACCEPTED_SOURCE_ID = "noaa-swpc-proton-flux"

#: Пороги уровня S NOAA, pfu — main-prompt.md §11, таблица «Механизм 1»
#: (дословно, происхождение — sources.yaml → space_weather[0] и
#: main-prompt.md §11, а не подбор этой задачи).
S1_THRESHOLD_PFU = 10.0
S2_THRESHOLD_PFU = 100.0
S3_THRESHOLD_PFU = 1000.0

#: Тот же порядок имён, что ``contracts/result.schema.json`` →
#: ``exceedance_hours_by_level`` для ``mechanism == "space_weather"`` и
#: ``src/domain/windows/dominance.py::_LEVEL_ORDER["space_weather"]``.
LEVEL_ORDER: tuple[str, ...] = ("background", "S1", "S2", "S3")
EXCEEDANCE_LEVELS: tuple[str, ...] = ("S1", "S2", "S3")


class UnsupportedRecordError(ValueError):
    """Запись — не наблюдение потока протонов GOES этой линии (неверный
    ``source_id``/``record_kind``/``unit``). Поднимается вместо тихого
    приведения типов — смешение наблюдения с чужой записью (например
    суточным прогнозом NOAA 3-Day, тоже Механизм 1, но другая величина) было
    бы прямым нарушением main-prompt.md §4 «различие должно быть невозможно
    потерять при передаче»."""


def classify_level(value: float) -> str:
    """Классифицирует один отсчёт потока (pfu) по шкале S NOAA.

    Пороги — включительно снизу (main-prompt.md §11 «10 и выше», «100 и
    выше», «1000 и выше»): значение РОВНО НА пороге уже принадлежит
    следующему уровню, не предыдущему.
    """
    if value >= S3_THRESHOLD_PFU:
        return "S3"
    if value >= S2_THRESHOLD_PFU:
        return "S2"
    if value >= S1_THRESHOLD_PFU:
        return "S1"
    return "background"


@dataclass(frozen=True)
class ObservedProtonSample:
    """Один нормализованный отсчёт наблюдения — как уже сохранён
    ``src/sources/swpc.py`` (``record.schema.json``), без переинтерпретации
    единиц или времени."""

    observed_at: datetime
    value: float | None  # pfu; None — заведомо невалидный/отсутствующий отсчёт
    quality: str  # "nominal" | "degraded" | "unknown" (contracts/record.schema.json)
    record_id: str


def observed_proton_samples_from_records(
    records: Iterable[Mapping[str, Any]],
) -> list[ObservedProtonSample]:
    """Преобразует уже выбранные (например через
    ``src.store.select_observed_range``) записи хранилища в
    :class:`ObservedProtonSample`.

    Отклоняет любую запись не из зарегистрированной линии наблюдения потока
    протонов GOES (:class:`UnsupportedRecordError`) — вторая граница после
    фильтра выборки, не единственная (тот же принцип, что и
    ``external_forecast.py::external_forecast_days_from_records``).
    """
    samples: list[ObservedProtonSample] = []
    for record in records:
        source_id = record.get("source_id")
        if source_id != _ACCEPTED_SOURCE_ID:
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has source_id={source_id!r}, "
                f"expected {_ACCEPTED_SOURCE_ID!r}"
            )
        if record.get("record_kind") != "observation":
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has record_kind="
                f"{record.get('record_kind')!r}, expected 'observation'"
            )
        unit = record.get("unit")
        value = record.get("value")
        # src/sources/swpc.py::to_record_input хранит заведомо невалидный/
        # отсутствующий отсчёт как value=None, unit=None (а не как 0 pfu,
        # main-prompt.md §2) — обе стороны этой пары обязаны совпадать, иначе
        # это не штатный пропуск, а искажённая нормализация выше по пайплайну.
        if value is None:
            if unit is not None:
                raise UnsupportedRecordError(
                    f"record {record.get('record_id')!r} has value=None but unit={unit!r} "
                    "(expected unit=None for a missing/invalid reading)"
                )
        elif unit != "pfu":
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has unit={unit!r}, expected 'pfu' "
                "(main-prompt.md §2: unit travels with the value)"
            )
        observed_at = datetime.fromisoformat(str(record["observed_at"]).replace("Z", "+00:00"))
        samples.append(
            ObservedProtonSample(
                observed_at=observed_at,
                value=float(value) if value is not None else None,
                quality=str(record.get("quality", "unknown")),
                record_id=str(record["record_id"]),
            )
        )
    return samples


@dataclass(frozen=True)
class ObservedFluxAssessment:
    """Представление наблюдаемого потока протонов GOES для одного окна ВКД —
    покрытие, уровни и суммарная длительность превышения (main-prompt.md §11
    «Оценка окна»: «максимальный уровень», «суммарная длительность
    превышения каждого порога», «полнота данных»)."""

    window_start: datetime
    window_end: datetime
    now: datetime
    beyond_horizon: bool
    max_level: str | None
    exceedance_hours_by_level: dict[str, float]
    coverage_fraction: float
    critical_gap: bool
    notes: tuple[str, ...]
    record_ids: tuple[str, ...]


def assess_observed_flux(
    samples: Iterable[ObservedProtonSample],
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    hold_seconds: float,
) -> ObservedFluxAssessment:
    """Строит покрытие/уровни окна ВКД по отдельным точечным отсчётам.

    ``hold_seconds`` — как долго один отсчёт считается представительным для
    периода ПОСЛЕ своего ``observed_at`` (main-prompt.md §7 «пороги,
    горизонты, шаги... в конфиге, не в коде»): вызывающая сторона передаёт
    сюда ``ttl_seconds`` того же источника из ``sources.yaml``
    (``space_weather[0].freshness.ttl_seconds`` — ожидаемый интервал между
    отсчётами реального продукта, документированный там же как «обновляется
    каждые ~1–5 минут»). Отсчёт считается представительным до МЕНЬШЕГО из:
    следующего РЕАЛЬНОГО отсчёта (если он ближе) и ``observed_at +
    hold_seconds`` — так соседние реальные 5-минутные отсчёты образуют
    сплошное покрытие, а пробел в поступлении данных длиннее ожидаемого шага
    НЕ маскируется продлением последнего значения на весь остаток окна (это
    и была бы запрещённая экстраполяция, см. докстринг модуля). Дополнительно
    отсчёт никогда не продлевается за ``now`` — представление о будущем
    моменте по одному лишь прошлому наблюдению не было бы наблюдением.

    ``coverage_fraction``/``critical_gap`` — той же семантики, что и в
    ``external_forecast.py::assess_external_forecast``: доля окна, покрытая
    пригодными (``value is not None``) отсчётами, и признак ЛЮБОГО, даже
    частичного, непокрытия (main-prompt.md §2 «частично покрыто» не выдаётся
    за «полностью покрыто»). Окно, выходящее за ``now`` хотя бы частично,
    поэтому тоже несёт ``critical_gap=True`` — это отражает
    ``beyond_horizon``, а не отказ источника; финальный код статуса
    (``ok``/``beyond_horizon``/``missing_data``/``stale_data``/
    ``source_error``) решает вызывающая сторона (``src/api/service.py``),
    которой одной известны статус попытки получения и реестр источников
    (main-prompt.md §8 «получение не считает физику», здесь — обратное:
    расчёт не знает про сеть/реестр).
    """
    if window_start.tzinfo is None or window_end.tzinfo is None or now.tzinfo is None:
        raise ValueError("window_start/window_end/now must be timezone-aware UTC datetimes")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    if hold_seconds <= 0:
        raise ValueError("hold_seconds must be positive")

    window_duration = window_end - window_start
    hold = timedelta(seconds=hold_seconds)

    # Дедупликация по ``observed_at`` (может произойти только если два
    # разных провайдерских ключа — например разные спутники — дали отсчёт на
    # один и тот же момент; штатно у этого источника такого не бывает, см.
    # sources.yaml, но вызывающая сторона не обязана была это исключить):
    # консервативно — берётся БОЛЬШЕЕ из значений. Для оценки радиационной
    # обстановки, где полнота данных важнее среднего, это безопаснее, чем
    # произвольный выбор одной из двух версий одного момента.
    latest_by_moment: dict[datetime, ObservedProtonSample] = {}
    for sample in samples:
        current = latest_by_moment.get(sample.observed_at)
        if current is None:
            latest_by_moment[sample.observed_at] = sample
        elif sample.value is not None and (current.value is None or sample.value > current.value):
            latest_by_moment[sample.observed_at] = sample

    ordered = sorted(latest_by_moment.values(), key=lambda s: s.observed_at)

    threshold_pfu_by_level = {
        "S1": S1_THRESHOLD_PFU, "S2": S2_THRESHOLD_PFU, "S3": S3_THRESHOLD_PFU,
    }
    covered_duration = timedelta(0)
    exceedance: dict[str, timedelta] = {level: timedelta(0) for level in EXCEEDANCE_LEVELS}
    max_level: str | None = None
    contributing_record_ids: list[str] = []
    degraded_count = 0

    for index, sample in enumerate(ordered):
        if sample.value is None:
            continue  # отсутствующий/невалидный отсчёт — пробел, main-prompt.md §2
        next_observed_at = ordered[index + 1].observed_at if index + 1 < len(ordered) else None
        cap = sample.observed_at + hold
        segment_end = min(cap, next_observed_at) if next_observed_at is not None else cap
        segment_end = min(segment_end, now)  # наблюдение не продлевается в будущее
        seg_start = max(sample.observed_at, window_start)
        seg_end = min(segment_end, window_end)
        if seg_end <= seg_start:
            continue

        duration = seg_end - seg_start
        covered_duration += duration
        contributing_record_ids.append(sample.record_id)
        if sample.quality == "degraded":
            degraded_count += 1

        level = classify_level(sample.value)
        if max_level is None or LEVEL_ORDER.index(level) > LEVEL_ORDER.index(max_level):
            max_level = level
        for threshold_level in EXCEEDANCE_LEVELS:
            if sample.value >= threshold_pfu_by_level[threshold_level]:
                exceedance[threshold_level] += duration

    assert covered_duration <= window_duration + timedelta(microseconds=1)
    coverage_fraction = min(1.0, covered_duration / window_duration)
    beyond_horizon = window_end > now
    critical_gap = covered_duration < window_duration

    notes: list[str] = [
        "Наблюдение GOES: поток протонов >=10 МэВ (pfu), шкала S NOAA — "
        "фон <10, S1>=10, S2>=100, S3>=1000 pfu (main-prompt.md §11, Механизм 1; "
        "sources.yaml → space_weather[0])."
    ]
    if not ordered:
        notes.append("Ни одного отсчёта наблюдения не пересекается с этим окном.")
    elif max_level is not None:
        hours_by_level = {k: v.total_seconds() / 3600.0 for k, v in exceedance.items()}
        notes.append(
            f"В покрытой части окна максимальный уровень — {max_level}; суммарная "
            f"длительность превышения порогов: {hours_by_level}."
        )
    if beyond_horizon:
        horizon_note = (
            f"Часть окна после {now.isoformat()} — за горизонтом наблюдения: GOES отдаёт "
            "мгновенный отсчёт, не прогноз, поэтому этот период «не покрыто», а не "
            "«спокойно» (main-prompt.md §4). Обоснованный внешний прогноз минимум на 6 "
            "часов вперёд — отдельная линия NOAA 3-Day Forecast (см. notes этого же "
            "механизма)."
        )
        notes.append(horizon_note)
    elif critical_gap:
        notes.append(
            f"Окно покрыто наблюдением только частично ({coverage_fraction:.0%} "
            "длительности) — непокрытая часть не имеет пригодного отсчёта в пределах "
            "ожидаемого шага между измерениями; это критический пробел данных, а не "
            "спокойная обстановка (main-prompt.md §2)."
        )
    if degraded_count:
        notes.append(
            f"{degraded_count} использованный(х) отсчёт(ов) получен во время манёвра "
            "yaw-flip спутника GOES (quality=degraded) — менее надёжен, но не исключён "
            "из оценки (sources.yaml → space_weather[0].quality_notes)."
        )

    return ObservedFluxAssessment(
        window_start=window_start,
        window_end=window_end,
        now=now,
        beyond_horizon=beyond_horizon,
        max_level=max_level,
        exceedance_hours_by_level={k: v.total_seconds() / 3600.0 for k, v in exceedance.items()},
        coverage_fraction=coverage_fraction,
        critical_gap=critical_gap,
        notes=tuple(notes),
        record_ids=tuple(contributing_record_ids),
    )


__all__ = [
    "EXCEEDANCE_LEVELS",
    "LEVEL_ORDER",
    "S1_THRESHOLD_PFU",
    "S2_THRESHOLD_PFU",
    "S3_THRESHOLD_PFU",
    "ObservedFluxAssessment",
    "ObservedProtonSample",
    "UnsupportedRecordError",
    "assess_observed_flux",
    "classify_level",
    "observed_proton_samples_from_records",
]
