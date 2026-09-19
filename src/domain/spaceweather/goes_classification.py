"""Механизм 1: классификация наблюдаемого потока GOES (>=10 MeV, pfu) по
шкале S NOAA для одного окна ВКД (FN-38, закрывает разрыв, выявленный при
подготовке FN-37).

Пороги — main-prompt.md §11 «Механизм 1. Радиационная обстановка», таблица
шкалы S NOAA (фиксирована постановкой, «не подбираются»); те же значения
зарегистрированы как ``classification_thresholds`` источника
``noaa-swpc-proton-flux`` в ``sources.yaml`` — происхождение прослеживается
из конфигурации, а не только из этого модуля (main-prompt.md §7 «пороги ...
в конфиге, а не в коде»). Порядок уровней и ключи ``exceedance_hours_by_level``
совпадают с ``src/domain/windows/dominance.py::_LEVEL_ORDER["space_weather"]``
и с ``contracts/result.schema.json``.

Чистый расчётный модуль (main-prompt.md §8 «расчёт не ходит в сеть»):
принимает уже выбранные записи хранилища (``src/store/records.py``,
форма ``contracts/record.schema.json``) как обычные словари — никакого HTTP
или прямого доступа к ``sqlite3.Connection`` здесь нет.

Ключевое отличие от ``external_forecast.py`` (та же папка, FN-31): это —
НАБЛЮДЕНИЕ, а не прогноз. Наблюдение по определению не покрывает будущее:
для окна ВКД (обычно начинающегося в будущем относительно ``now``)
классификация покрывает только отрезок ``[window_start, min(window_end,
now))`` — период после ``now`` внутри окна остаётся некрытым, что даёт
``critical_gap=True`` почти для любого реалистичного окна ВКД (главный
прогнозный вклад в Механизм 1 — отдельная, уже подключённая линия NOAA
3-Day Forecast, ``external_forecast.py``; обе линии объединяются в одну
``mechanismAssessment`` вызывающей стороной, ``src/api/service.py``).

Пропуск (``value=None`` — невалидный/отсутствующий отсчёт продукта) не
подменяется фоновым уровнем и не удерживает предыдущее известное значение
вперёд «сквозь» себя: соответствующий отрезок времени просто не входит в
покрытие (main-prompt.md §2 «пропуск не заменяется ... последним известным
... значением»).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc

#: Единственный источник, чьи записи эта интерпретация вправе читать — та же
#: граница, что и в ``external_forecast.py`` (main-prompt.md §4 «различие
#: должно быть невозможно потерять при передаче»): функция отказывается
#: интерпретировать суточную вероятность внешнего прогноза или чужую запись
#: как наблюдение GOES pfu.
_ACCEPTED_SOURCE_IDS = frozenset({"noaa-swpc-proton-flux"})

#: Возрастающий порядок уровней шкалы S NOAA — те же слова, что и
#: ``dominance.py::_LEVEL_ORDER["space_weather"]``.
LEVEL_ORDER: tuple[str, ...] = ("background", "S1", "S2", "S3")

#: Ключи ``exceedance_hours_by_level`` контракта для space_weather —
#: "background" в них не входит (contracts/result.schema.json).
EXCEEDANCE_LEVELS: tuple[str, ...] = ("S1", "S2", "S3")

#: Пороги шкалы S NOAA в pfu (main-prompt.md §11). Зеркалируются в
#: sources.yaml → noaa-swpc-proton-flux → classification_thresholds для
#: прослеживаемости происхождения из конфигурации (приёмка FN-38, п.1).
THRESHOLDS_PFU: dict[str, float] = {"S1": 10.0, "S2": 100.0, "S3": 1000.0}


class UnsupportedRecordError(ValueError):
    """Запись — не наблюдение GOES pfu (неверный source_id/record_kind/unit).

    Поднимается вместо тихого приведения типов — смешение наблюдения и
    прогноза на этом слое было бы прямым нарушением main-prompt.md §4."""


def classify_level(value_pfu: float) -> str:
    """Классифицирует один отсчёт потока (>=10 MeV, pfu) по шкале S NOAA.

    Пороги main-prompt.md §11: фон — ниже 10; S1 — 10 и выше; S2 — 100 и
    выше; S3 — 1000 и выше. Границы включительны снизу (10.0 pfu уже S1, не
    фон) — так же, как задокументировано в таблице постановки.
    """
    if value_pfu >= THRESHOLDS_PFU["S3"]:
        return "S3"
    if value_pfu >= THRESHOLDS_PFU["S2"]:
        return "S2"
    if value_pfu >= THRESHOLDS_PFU["S1"]:
        return "S1"
    return "background"


@dataclass(frozen=True)
class GoesPfuSample:
    """Один сохранённый отсчёт наблюдения GOES pfu, как он есть в хранилище —
    без интерпретации уровня."""

    observed_at: datetime
    value: float | None
    record_id: str
    degraded: bool


def goes_samples_from_records(records: Iterable[Mapping[str, Any]]) -> list[GoesPfuSample]:
    """Преобразует уже выбранные записи хранилища (форма
    ``contracts/record.schema.json``) в :class:`GoesPfuSample`, отсортированные
    по ``observed_at`` по возрастанию.

    Отклоняет любую запись не из зарегистрированной линии наблюдения GOES pfu
    (:class:`UnsupportedRecordError`) — та же вторая граница, что и в
    ``external_forecast.py::external_forecast_days_from_records``.
    """
    samples: list[GoesPfuSample] = []
    for record in records:
        source_id = record.get("source_id")
        if source_id not in _ACCEPTED_SOURCE_IDS:
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has source_id={source_id!r}, "
                f"expected one of {sorted(_ACCEPTED_SOURCE_IDS)}"
            )
        if record.get("record_kind") != "observation":
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has record_kind="
                f"{record.get('record_kind')!r}, expected 'observation'"
            )
        value = record.get("value")
        unit = record.get("unit")
        if value is not None and unit != "pfu":
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has unit={unit!r}, expected 'pfu' "
                "for a non-null observation (main-prompt.md §2: unit travels with the value)"
            )
        observed_at = datetime.fromisoformat(str(record["observed_at"]).replace("Z", "+00:00"))
        samples.append(
            GoesPfuSample(
                observed_at=observed_at,
                value=float(value) if value is not None else None,
                record_id=str(record["record_id"]),
                degraded=(record.get("quality") == "degraded"),
            )
        )
    samples.sort(key=lambda s: s.observed_at)
    return samples


@dataclass(frozen=True)
class GoesClassificationAssessment:
    """Готовая интерпретация наблюдения GOES pfu для одного окна ВКД — форма,
    из которой строится ``mechanismAssessment`` (contracts/result.schema.json)
    для ``mechanism = "space_weather"``."""

    status: str
    max_level: str | None
    exceedance_hours_by_level: dict[str, float] | None
    coverage_fraction: float
    critical_gap: bool
    notes: tuple[str, ...]
    record_ids: tuple[str, ...]


def _missing(status: str, note: str) -> GoesClassificationAssessment:
    return GoesClassificationAssessment(
        status=status,
        max_level=None,
        exceedance_hours_by_level=None,
        coverage_fraction=0.0,
        critical_gap=True,
        notes=(note,),
        record_ids=(),
    )


def assess_goes_classification(
    samples: Sequence[GoesPfuSample],
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    critical_staleness_seconds: float,
    source_currently_unavailable: bool = False,
) -> GoesClassificationAssessment:
    """Строит классификацию наблюдения GOES pfu для окна ``[window_start,
    window_end)``.

    ``status`` — одно из ``mechanismStatus`` (contracts/result.schema.json),
    без «благоприятной» подмены ни в одном случае (main-prompt.md §2):

    - ``source_error`` — записей нет вовсе, и именно ЭТА попытка получения
      источника завершилась отказом (таймаут/квота/формат) — отказ
      источника, а не «нет данных вообще никогда».
    - ``missing_data`` — либо записей нет вовсе (и источник не отказывал
      именно сейчас — например ещё ни разу не получен), либо окно/его
      покрытая (см. ниже) часть не пересекается ни с одним пригодным
      отсчётом (в т.ч. окно целиком в будущем относительно последнего
      наблюдения, или пересекающиеся отсчёты — сплошь пропуски).
    - ``stale_data`` — самый свежий отсчёт старше ``critical_staleness_seconds``
      относительно ``now`` — наблюдению нельзя доверять как «текущему»
      состоянию, независимо от того, что можно было бы формально посчитать.
    - ``ok`` — есть хотя бы частичное валидное покрытие окна; при этом
      ``critical_gap=True`` остаётся, если покрыта не вся длительность окна
      (то же правило, что ``external_forecast.py::assess_external_forecast``)
      — частичное покрытие внешне неотличимо от полного без этого флага.

    Наблюдение принципиально не покрывает будущее: покрывается только
    ``[window_start, min(window_end, now))`` — остаток окна (обычно
    бо́льшая его часть, main-prompt.md §4 «за горизонтом — не покрыто, а не
    спокойно», применено здесь к наблюдению, не к прогнозу) не входит в
    ``coverage_fraction`` и не может быть заполнен экстраполяцией
    последнего значения — это была бы подмена наблюдения предположением.

    Внутри покрытого отрезка значение отсчёта действует с момента его
    наблюдения до следующего отсчёта (любого, включая пропуск) — период до
    первого известного отсчёта и любой отрезок, управляемый отсчётом-
    пропуском (``value=None``), не засчитывается в покрытие вовсе (не
    подменяется ни фоном, ни удержанием предыдущего значения).
    """
    if window_start.tzinfo is None or window_end.tzinfo is None or now.tzinfo is None:
        raise ValueError("window_start/window_end/now must be timezone-aware UTC datetimes")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    window_seconds = (window_end - window_start).total_seconds()

    if not samples:
        if source_currently_unavailable:
            return _missing(
                "source_error",
                "Нет ни одной сохранённой записи наблюдения GOES pfu (>=10 MeV), и "
                "текущая попытка получения источника завершилась отказом — оценить "
                "механизм 1 по наблюдению для этого окна невозможно (main-prompt.md "
                "§2: отказ источника не даёт благоприятную оценку).",
            )
        return _missing(
            "missing_data",
            "Нет ни одной сохранённой записи наблюдения GOES pfu (>=10 MeV) — источник "
            "ещё ни разу не был успешно получен.",
        )

    freshest = max(samples, key=lambda s: s.observed_at)
    staleness_seconds = abs((now - freshest.observed_at).total_seconds())
    if staleness_seconds >= critical_staleness_seconds:
        return _missing(
            "stale_data",
            f"Последний сохранённый отсчёт GOES pfu — {freshest.observed_at.isoformat()} "
            f"({staleness_seconds / 3600:.2f} ч назад), это не младше порога критического "
            f"устаревания ({critical_staleness_seconds / 3600:.2f} ч, sources.yaml → "
            "noaa-swpc-proton-flux → freshness.critical_staleness_seconds) — наблюдение "
            "слишком устарело, чтобы описывать текущую обстановку (main-prompt.md §2).",
        )

    effective_end = min(window_end, now)
    if effective_end <= window_start:
        return _missing(
            "missing_data",
            "Окно целиком находится в будущем относительно последнего наблюдения GOES "
            "pfu — наблюдение по определению не покрывает ещё не наступившее время "
            "(main-prompt.md §4 «за горизонтом — не покрыто, а не спокойно»).",
        )

    boundaries = sorted(
        {s.observed_at for s in samples if window_start < s.observed_at < effective_end}
    )
    points = sorted({window_start, effective_end, *boundaries})

    covered_seconds_by_level: dict[str, float] = {level: 0.0 for level in LEVEL_ORDER}
    used_record_ids: set[str] = set()
    degraded_used = False
    covered_usable_seconds = 0.0

    samples_sorted = sorted(samples, key=lambda s: s.observed_at)
    for seg_start, seg_end in zip(points, points[1:], strict=False):
        seg_seconds = (seg_end - seg_start).total_seconds()
        if seg_seconds <= 0:
            continue
        governing = None
        for sample in samples_sorted:
            if sample.observed_at <= seg_start:
                governing = sample
            else:
                break
        if governing is None or governing.value is None:
            continue
        level = classify_level(governing.value)
        covered_usable_seconds += seg_seconds
        for lvl in LEVEL_ORDER:
            if LEVEL_ORDER.index(lvl) <= LEVEL_ORDER.index(level):
                covered_seconds_by_level[lvl] += seg_seconds
        used_record_ids.add(governing.record_id)
        degraded_used = degraded_used or governing.degraded

    if covered_usable_seconds <= 0:
        return _missing(
            "missing_data",
            "Окно пересекается по времени с попытками наблюдения GOES pfu, но все "
            "относящиеся к нему отсчёты — пропуски (нет валидного значения потока); "
            "main-prompt.md §2: пропуск не заменяется фоновым значением.",
        )

    max_level = LEVEL_ORDER[
        max(i for i, lvl in enumerate(LEVEL_ORDER) if covered_seconds_by_level[lvl] > 0)
    ]
    exceedance_hours_by_level = {
        level: covered_seconds_by_level[level] / 3600.0 for level in EXCEEDANCE_LEVELS
    }
    coverage_fraction = covered_usable_seconds / window_seconds
    critical_gap = covered_usable_seconds < window_seconds

    notes = [
        f"Наблюдение GOES pfu (>=10 MeV, шкала S NOAA main-prompt.md §11): максимальный "
        f"уровень в покрытой части окна — {max_level}; покрыто {coverage_fraction:.0%} "
        "длительности окна валидным наблюдением. Наблюдение описывает только уже "
        "произошедшее/самое недавнее известное значение, не будущее."
    ]
    if critical_gap:
        notes.append(
            "Непокрытая часть окна (будущее время и/или отсчёты-пропуски) не имеет "
            "наблюдения — это критический пробел данных, а не спокойная обстановка "
            "(main-prompt.md §2)."
        )
    if degraded_used:
        notes.append(
            "Часть учтённых отсчётов получена в интервале манёвра yaw-flip спутника "
            "GOES (quality=degraded, sources.yaml) — измерение менее надёжно, хотя и "
            "не отсутствует."
        )

    return GoesClassificationAssessment(
        status="ok",
        max_level=max_level,
        exceedance_hours_by_level=exceedance_hours_by_level,
        coverage_fraction=coverage_fraction,
        critical_gap=critical_gap,
        notes=tuple(notes),
        record_ids=tuple(sorted(used_record_ids)),
    )


def to_mechanism_assessment_dict(assessment: GoesClassificationAssessment) -> dict[str, Any]:
    """Сериализует :class:`GoesClassificationAssessment` в форму
    ``mechanismAssessment`` (contracts/result.schema.json) для
    ``mechanism = "space_weather"``."""
    return {
        "mechanism": "space_weather",
        "status": assessment.status,
        "max_level": assessment.max_level,
        "exceedance_hours_by_level": (
            dict(assessment.exceedance_hours_by_level)
            if assessment.exceedance_hours_by_level is not None
            else None
        ),
        "coverage_fraction": assessment.coverage_fraction,
        "critical_gap": assessment.critical_gap,
        "notes": list(assessment.notes),
        "record_ids": list(assessment.record_ids),
    }


__all__ = [
    "EXCEEDANCE_LEVELS",
    "LEVEL_ORDER",
    "THRESHOLDS_PFU",
    "GoesClassificationAssessment",
    "GoesPfuSample",
    "UnsupportedRecordError",
    "assess_goes_classification",
    "classify_level",
    "goes_samples_from_records",
    "to_mechanism_assessment_dict",
]
