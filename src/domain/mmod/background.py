"""``ratio_to_background`` и пороги 1.2/2 Механизма 2 (MMOD) — FN-39 (S2-08).

Закрывает научный gate FN-32: числитель/знаменатель ``ratio_to_background``
даёт УЖЕ согласованно отнормированный первичный источник
(``sources.yaml#nasa-meo-leo-forecast-2024``, NASA MEO), не эвристический
коэффициент (приёмка FN-39 п.1 «эвристический коэффициент запрещён»).
Пороги 1.2/2, классифицирующие это отношение на Фон/Повышенный/Выраженный,
остаются эвристикой КОМАНДЫ поверх честного отношения — это прямо разрешено
и требуется помечать так (.ai/main-prompt.md §11 «Уровни — эвристика
команды, так и помечаются в объяснениях»); отличие от запрещённого —
эвристика здесь применяется к уже доказанному числу, не заменяет его.

Чистый расчётный модуль (.ai/main-prompt.md §8): принимает уже отобранные
(например через ``select_as_of``) записи источника ``nasa-meo-leo-forecast-2024``
как обычные словари/дата-классы, не ходит в сеть и не читает хранилище —
получение и нормализация в запись — ``src/sources/mmod.py``.

**Единственная линия этой оценки** — ``factor 1.05e+02 J`` того источника
(решение владельца задачи FN-39); функции здесь отклоняют запись любого
другого ``source_id``/``unit``, тем же паттерном, что и
``src/domain/spaceweather/external_forecast.py::_ACCEPTED_SOURCE_IDS``.

**Интерполяция между часовыми узлами — явно версионирована**
(:data:`INTERPOLATION_VERSION`) и линейна по ``factor_105j`` (эквивалентно
линейной по ``ratio_to_background``, так как последнее — аффинное
преобразование первого): решение владельца задачи FN-39, ``sources.yaml``.

**Граница «критический пробел».** Контракт (``contracts/result.schema.json``)
требует ``max_level``/``exceedance_hours_by_level`` равными ``null``, когда
``status != "ok"``. Эта задача не выдаёт частично посчитанные числа при
неполном покрытии окна гридом (main-prompt.md §2 «отказ… никогда не
превращается в благоприятную оценку», тот же принцип — и в частично
благоприятную): окно, покрытое гридом НЕ ЦЕЛИКОМ, получает
``status="missing_data"``, ``max_level=None``,
``exceedance_hours_by_level=None`` — даже если бо́льшая часть окна покрыта.
``coverage_fraction`` и заметки при этом остаются информативными (О4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from src.sources.mmod import SOURCE_ID, VALUE_UNIT

UTC = timezone.utc

Level = Literal["background", "elevated", "pronounced"]

#: Пороги main-prompt.md §11 «Механизм 2. MMOD»: Фон ниже 1.2, Повышенный от
#: 1.2 до 2 включительно («до 2» — верхняя граница включена, тем же способом,
#: что нижняя граница «от 1,2»), Выраженный строго выше 2 («выше» — граница
#: исключена). Третий элемент кортежа — включена ли сама граница в этот
#: уровень (``True`` ⇒ ``ratio >= threshold``, ``False`` ⇒ ``ratio >
#: threshold``); без этого ``ratio == 2.0`` было бы неотличимо между
#: «Повышенный» и «Выраженный» в зависимости от порядка сравнения. Порядок —
#: возрастающий, используется и для max_level, и для
#: exceedance_hours_by_level (пороги ВЛОЖЕНЫ, как шкала S NOAA у Механизма 1:
#: время выше 2 — подмножество времени выше 1.2, contracts/result.schema.json
#: комментарий к exceedance_hours_by_level).
_THRESHOLDS: tuple[tuple[Level, float, bool], ...] = (
    ("elevated", 1.2, True),
    ("pronounced", 2.0, False),
)

#: Версия правила интерполяции между соседними часовыми узлами — решение
#: владельца задачи FN-39 (линейная по факту публикации поставщика, не
#: физическая модель). Любое изменение правила обязано менять эту строку
#: (по аналогии с ``src/domain/orbit/interpolate.py`` hermite-cubic-per-axis-v1).
INTERPOLATION_VERSION = "linear-per-hour-v1"


class UnsupportedMmodBackgroundRecordError(ValueError):
    """Запись — не почасовой ``factor_105j`` NASA MEO (неверный source_id/
    record_kind/unit). Поднимается вместо тихого приведения типов
    (.ai/main-prompt.md §4), тем же паттерном, что и
    ``src/domain/spaceweather/external_forecast.py::UnsupportedRecordError``."""


def ratio_to_background(factor_105j: float) -> float:
    """``ratio_to_background = 1 + factor_105j`` — числитель/знаменатель уже
    согласованно отнормированы поставщиком (см. docstring модуля); эта
    функция ничего не подбирает и не перенормирует, только переводит
    опубликованный ``factor`` (доля повышения) в отношение «показатель /
    фон» (``main-prompt.md`` §11 «относительное превышение потока… над
    спорадическим фоном»)."""
    return 1.0 + factor_105j


def classify_level(ratio: float) -> Level:
    """Фон/Повышенный/Выраженный по порогам main-prompt.md §11 (см.
    :data:`_THRESHOLDS`) — эвристика команды поверх честного
    ``ratio_to_background``, не эвристика самого отношения."""
    level: Level = "background"
    for candidate_level, threshold, inclusive in _THRESHOLDS:
        if (ratio >= threshold) if inclusive else (ratio > threshold):
            level = candidate_level
    return level


@dataclass(frozen=True)
class BackgroundNode:
    """Один часовой узел сетки NASA MEO, как получен из хранилища (уже
    отфильтрован по ``source_id``/``record_kind``/``unit``)."""

    at: datetime
    factor_105j: float
    record_id: str


def background_nodes_from_records(records: Sequence[Mapping[str, Any]]) -> list[BackgroundNode]:
    """Преобразует уже выбранные (например через ``select_as_of``) записи
    хранилища (форма ``contracts/record.schema.json``) в узлы сетки,
    отсортированные по времени.

    Отклоняет любую запись не из ``nasa-meo-leo-forecast-2024``
    (:class:`UnsupportedMmodBackgroundRecordError`) — вторая граница поверх
    фильтра выборки, тот же принцип, что и у Механизма 1
    (``external_forecast_days_from_records``)."""
    nodes: list[BackgroundNode] = []
    for record in records:
        source_id = record.get("source_id")
        if source_id != SOURCE_ID:
            raise UnsupportedMmodBackgroundRecordError(
                f"record {record.get('record_id')!r} has source_id={source_id!r}, "
                f"expected {SOURCE_ID!r}"
            )
        if record.get("record_kind") != "forecast":
            raise UnsupportedMmodBackgroundRecordError(
                f"record {record.get('record_id')!r} has record_kind="
                f"{record.get('record_kind')!r}, expected 'forecast'"
            )
        if record.get("unit") != VALUE_UNIT:
            raise UnsupportedMmodBackgroundRecordError(
                f"record {record.get('record_id')!r} has unit={record.get('unit')!r}, "
                f"expected {VALUE_UNIT!r} (main-prompt.md §2: unit travels with the value)"
            )
        value = record["value"]
        if value is None:
            # Контракт допускает value=null (пропуск), main-prompt.md §2: не
            # подставляется 0 — узел просто не строится из этой записи.
            continue
        at_raw = str(record["valid_from"]).replace("Z", "+00:00")
        nodes.append(
            BackgroundNode(
                at=datetime.fromisoformat(at_raw),
                factor_105j=float(value),
                record_id=str(record["record_id"]),
            )
        )
    nodes.sort(key=lambda node: node.at)
    return nodes


@dataclass(frozen=True)
class MmodBackgroundAssessment:
    """Оценка ``ratio_to_background`` Механизма 2 для одного окна ВКД —
    без геометрии станции (см. docstring модуля: экранирование/скорость
    встречи объединяются отдельно, на уровне ``src/api/service.py``, как
    ``trajectory_context``, не здесь)."""

    window_start: datetime
    window_end: datetime
    status: Literal["ok", "missing_data"]
    max_level: Level | None
    exceedance_hours_by_level: dict[str, float] | None
    coverage_fraction: float
    critical_gap: bool
    notes: tuple[str, ...]
    record_ids: tuple[str, ...]


def _ratio_at(node_a: BackgroundNode, node_b: BackgroundNode, at: datetime) -> float:
    """Линейная интерполяция ``ratio_to_background`` между двумя соседними
    узлами (см. :data:`INTERPOLATION_VERSION`). ``at`` обязан лежать внутри
    ``[node_a.at, node_b.at]``; на самих узлах возвращает точное значение без
    ошибки округления интерполяции."""
    if at <= node_a.at:
        return ratio_to_background(node_a.factor_105j)
    if at >= node_b.at:
        return ratio_to_background(node_b.factor_105j)
    total = (node_b.at - node_a.at).total_seconds()
    frac = (at - node_a.at).total_seconds() / total
    factor = node_a.factor_105j + frac * (node_b.factor_105j - node_a.factor_105j)
    return ratio_to_background(factor)


def _segment_exceedance_seconds(
    t0: datetime, r0: float, t1: datetime, r1: float, threshold: float, *, inclusive: bool
) -> float:
    """Длительность (сек) внутри ``[t0, t1]`` линейного участка ``ratio(t)``
    (от ``r0`` в ``t0`` до ``r1`` в ``t1``), где ``ratio(t)`` удовлетворяет
    порогу (``>= threshold``, если ``inclusive``, иначе строго ``>
    threshold`` — см. :data:`_THRESHOLDS`).

    ``ratio`` линейна на участке (см. :func:`_ratio_at`), поэтому множество,
    удовлетворяющее порогу, — не более одного непрерывного подынтервала
    (включая пустой или весь участок целиком). Включённость границы влияет
    на результат только для вырожденного случая ``r0 == r1 == threshold``
    (участок целиком лежит НА границе) — единичная точка пересечения имеет
    нулевую меру и не меняет длительность ни при какой включённости."""
    total = (t1 - t0).total_seconds()
    if total <= 0.0:
        return 0.0

    def _meets(ratio: float) -> bool:
        return ratio >= threshold if inclusive else ratio > threshold

    if _meets(r0) and _meets(r1):
        return total
    if not _meets(r0) and not _meets(r1):
        return 0.0
    frac = (threshold - r0) / (r1 - r0)
    crossing = frac * total
    if not _meets(r0) and _meets(r1):
        return total - crossing
    return crossing


def assess_mmod_background(
    nodes: Sequence[BackgroundNode],
    *,
    window_start: datetime,
    window_end: datetime,
    document_grid_start: datetime | None = None,
    document_grid_end: datetime | None = None,
) -> MmodBackgroundAssessment:
    """Строит :class:`MmodBackgroundAssessment` для окна ``[window_start,
    window_end)`` из уже отобранных узлов сетки.

    ``nodes`` не обязан нести ВЕСЬ известный документ — вызывающая сторона
    (``src/sources/mmod.py::ensure_mmod_records_for_window``) вставляет и
    выбирает только узлы, нужные ДЛЯ ЭТОГО окна (main-prompt.md §8 «не
    строй инфраструктуру там, где её не нужно»), не все ~8791 часовых
    записей года. Корректность ``critical_gap``/``coverage_fraction`` от
    этого не зависит — они считаются по фактическому покрытию окна
    сегментами между соседними переданными узлами, не по сравнению границ
    окна с границами ``nodes``. ``document_grid_start``/``document_grid_end``
    — границы ПОЛНОГО документа (если известны вызывающей стороне) только
    для точного текста заметок; при их отсутствии используются границы
    переданных ``nodes`` (менее точно, если передано подмножество).

    Окно, не покрытое гридом ЦЕЛИКОМ (частично или полностью — до
    2024-01-01T00:00Z или после последнего узла), получает
    ``status="missing_data"``, ``max_level=None``,
    ``exceedance_hours_by_level=None`` — см. docstring модуля, «Граница
    «критический пробел»»."""
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window_start/window_end must be timezone-aware UTC datetimes")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    if not nodes:
        return MmodBackgroundAssessment(
            window_start=window_start,
            window_end=window_end,
            status="missing_data",
            max_level=None,
            exceedance_hours_by_level=None,
            coverage_fraction=0.0,
            critical_gap=True,
            notes=(
                "NASA MEO 2024 LEO forecast: нет ни одной записи "
                f"{SOURCE_ID!r} — окно не может быть оценено.",
            ),
            record_ids=(),
        )

    window_duration = (window_end - window_start).total_seconds()
    grid_start = document_grid_start if document_grid_start is not None else nodes[0].at
    grid_end = document_grid_end if document_grid_end is not None else nodes[-1].at

    covered_seconds = 0.0
    exceedance_seconds: dict[str, float] = {level: 0.0 for level, _, _ in _THRESHOLDS}
    used_record_ids: list[str] = []

    for node_a, node_b in zip(nodes, nodes[1:], strict=False):
        seg_start = max(node_a.at, window_start)
        seg_end = min(node_b.at, window_end)
        if seg_end <= seg_start:
            continue
        used_record_ids.append(node_a.record_id)
        used_record_ids.append(node_b.record_id)
        covered_seconds += (seg_end - seg_start).total_seconds()
        r_start = _ratio_at(node_a, node_b, seg_start)
        r_end = _ratio_at(node_a, node_b, seg_end)
        for level, threshold, inclusive in _THRESHOLDS:
            exceedance_seconds[level] += _segment_exceedance_seconds(
                seg_start, r_start, seg_end, r_end, threshold, inclusive=inclusive
            )

    coverage_fraction = min(1.0, covered_seconds / window_duration) if window_duration else 0.0
    critical_gap = covered_seconds < window_duration - 1e-6  # допуск на округление float

    notes: list[str] = [
        f"NASA MEO 2024 LEO forecast (factor 1.05e+02 J, {INTERPOLATION_VERSION}): "
        f"сетка покрывает {grid_start.isoformat()} .. {grid_end.isoformat()}."
    ]
    if window_start < grid_start or window_end > grid_end:
        notes.append(
            "Окно выходит за пределы покрытия годового прогноза NASA MEO на 2024 год — "
            "это отсутствие данных за пределами документа, а не спокойная обстановка "
            "(main-prompt.md §2)."
        )
    elif critical_gap:
        notes.append(
            "Окно внутри диапазона документа, но покрыто узлами не полностью — "
            "критический пробел данных."
        )

    if critical_gap:
        return MmodBackgroundAssessment(
            window_start=window_start,
            window_end=window_end,
            status="missing_data",
            max_level=None,
            exceedance_hours_by_level=None,
            coverage_fraction=coverage_fraction,
            critical_gap=True,
            notes=tuple(notes),
            record_ids=tuple(dict.fromkeys(used_record_ids)),
        )

    exceedance_hours = {level: seconds / 3600.0 for level, seconds in exceedance_seconds.items()}
    max_level: Level = "background"
    for level, _, _inclusive in _THRESHOLDS:
        if exceedance_hours[level] > 0.0:
            max_level = level
    notes.append(
        f"Максимальный уровень в окне: {max_level} "
        f"(Фон — эвристика команды поверх ratio_to_background, main-prompt.md §11; "
        "ниже 1.2 — Фон, 1.2..2 — Повышенный, выше 2 — Выраженный)."
    )

    return MmodBackgroundAssessment(
        window_start=window_start,
        window_end=window_end,
        status="ok",
        max_level=max_level,
        exceedance_hours_by_level=exceedance_hours,
        coverage_fraction=coverage_fraction,
        critical_gap=False,
        notes=tuple(notes),
        record_ids=tuple(dict.fromkeys(used_record_ids)),
    )


__all__ = [
    "INTERPOLATION_VERSION",
    "Level",
    "BackgroundNode",
    "MmodBackgroundAssessment",
    "UnsupportedMmodBackgroundRecordError",
    "ratio_to_background",
    "classify_level",
    "background_nodes_from_records",
    "assess_mmod_background",
]
