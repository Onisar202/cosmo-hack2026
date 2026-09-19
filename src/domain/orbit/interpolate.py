"""Интерполяция траектории МКС по векторам состояния OEM (FN-33, S2-03).

OEM не даёт среднеэлементного набора для SGP4 — источник
(``src/sources/orbit_history.py``) уже отдаёт готовые векторы состояния
(положение + скорость) на неравномерной сетке узлов: номинальный шаг 240 с,
но вокруг манёвров — плотные ~2-секундные участки (см.
``tests/fixtures/orbit/history/README.md``). Положение/скорость между узлами
восстанавливается интерполяцией, а не физической моделью распространения —
чистая функция без сети и без хранилища (.ai/main-prompt.md §8): принимает
уже разобранные узлы и момент времени аргументами, не читает "сейчас".
Получение и разбор OEM — ``src/sources/orbit_history.py``; этот модуль их не
знает.

**Метод и его версия** (.ai/main-prompt.md §3 «каждый результат несёт
algorithm_version»): :data:`INTERPOLATION_METHOD` =
``"hermite-cubic-per-axis-v1"`` — кубическая интерполяция Эрмита по каждой
координатной оси отдельно, используя заданную в узле скорость как
производную положения. Выбор обоснован тем, что каждый узел OEM несёт ГОТОВУЮ
производную (в отличие от последовательности одних лишь положений, где
производную пришлось бы оценивать конечными разностями по соседям — метод
менее точный и хуже определённый на неравномерной сетке): интерполянт точно
воспроизводит и положение, и скорость в каждом узле (значение по построению
совпадает на границах отрезка), деградирует плавно при неравномерном шаге и
не требует новой зависимости — ``scipy`` не входит в ``pyproject.toml``,
реализация на чистом Python поверх уже используемых типов.

**Экстраполяция запрещена.** Запрос времени вне
``[nodes[0].time, nodes[-1].time]`` поднимает :class:`InterpolationError`, а
не подставляет ближайшее значение молча — главный запрет задачи (правило
временной честности не менее строгое, чем правило "историческое не
подменяется современным", main-prompt.md §1/§11: неизвестное положение вне
охваченного интервала не становится придуманным).

Система координат — EME2000 (собственный выход OEM), единицы — километры
для положения, километры в секунду для скорости, без скрытого преобразования
(.ai/main-prompt.md §2 «единицы хранятся рядом со значением»). EME2000
совпадает с системой, в которой заданы радианты MMOD (``src/domain/mmod``) —
преобразование в TEME здесь не требуется и не делается (TEME — только у
SGP4/TLE-пути ``src/domain/orbit/propagate.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

INTERPOLATION_METHOD = "hermite-cubic-per-axis-v1"
COORDINATE_SYSTEM = "EME2000"
POSITION_UNIT = "km"
VELOCITY_UNIT = "km/s"


class InterpolationError(RuntimeError):
    """Интерполяция невозможна: узлов меньше двух, узлы не отсортированы
    строго по возрастанию времени, либо запрошенный момент лежит вне
    охваченного узлами интервала (запрещённая экстраполяция — см. модульный
    docstring)."""


@dataclass(frozen=True)
class OemNode:
    """Один узел OEM для интерполяции: момент, положение и скорость
    (EME2000, км, км/с) — форма идентична узлу, который разбирает
    ``src/sources/orbit_history.py::OemStateVectorRecord``, но этот модуль не
    импортирует ``sources`` (.ai/main-prompt.md §8) и определяет свой тип."""

    time: datetime
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]


@dataclass(frozen=True)
class InterpolatedState:
    """Положение/скорость станции на запрошенный момент, интерполированные
    между двумя узлами одного выпуска OEM."""

    time: datetime
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]
    coordinate_system: str = COORDINATE_SYSTEM
    method: str = INTERPOLATION_METHOD


def _require_aware_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be a timezone-aware UTC datetime (.ai/main-prompt.md §1)"
        )


def _hermite_basis(s: float) -> tuple[float, float, float, float]:
    """Базисные функции кубического интерполянта Эрмита на ``s`` in [0, 1]."""
    s2 = s * s
    s3 = s2 * s
    h00 = 2 * s3 - 3 * s2 + 1
    h10 = s3 - 2 * s2 + s
    h01 = -2 * s3 + 3 * s2
    h11 = s3 - s2
    return h00, h10, h01, h11


def _hermite_basis_derivative(s: float) -> tuple[float, float, float, float]:
    """Производные базисных функций по ``s`` (для скорости — см. ниже)."""
    s2 = s * s
    d00 = 6 * s2 - 6 * s
    d10 = 3 * s2 - 4 * s + 1
    d01 = -6 * s2 + 6 * s
    d11 = 3 * s2 - 2 * s
    return d00, d10, d01, d11


def _interpolate_axis(
    p0: float, v0: float, p1: float, v1: float, *, dt_seconds: float, s: float
) -> tuple[float, float]:
    """Интерполирует одну координатную ось между двумя узлами.

    ``dt_seconds`` масштабирует производную по ``s`` (безразмерный параметр
    отрезка) в производную по времени в секундах — без этого масштаба
    скорость в узлах не совпала бы с переданной (единицы км/с, а не
    км/безразмерный параметр)."""
    h00, h10, h01, h11 = _hermite_basis(s)
    d00, d10, d01, d11 = _hermite_basis_derivative(s)
    position = h00 * p0 + h10 * dt_seconds * v0 + h01 * p1 + h11 * dt_seconds * v1
    velocity = (d00 * p0 + d10 * dt_seconds * v0 + d01 * p1 + d11 * dt_seconds * v1) / dt_seconds
    return position, velocity


def interpolate_state(nodes: Sequence[OemNode], time: datetime) -> InterpolatedState:
    """Кубическая интерполяция Эрмита положения/скорости на момент ``time``.

    ``nodes`` — уже разобранные узлы одного выпуска OEM, отсортированные по
    времени по возрастанию (проверяется явно, а не молча допускается любой
    порядок — на неверно отсортированном входе бинарный поиск отрезка дал бы
    неверный, но правдоподобный результат вместо ошибки).

    Экстраполяция запрещена (см. модульный docstring): ``time`` вне
    ``[nodes[0].time, nodes[-1].time]`` поднимает :class:`InterpolationError`.
    """
    if len(nodes) < 2:
        raise InterpolationError(f"need at least 2 nodes to interpolate, got {len(nodes)}")
    _require_aware_utc(time, "time")
    for index, node in enumerate(nodes):
        _require_aware_utc(node.time, f"nodes[{index}].time")
    for previous, current in zip(nodes, nodes[1:], strict=False):
        if current.time <= previous.time:
            raise InterpolationError(
                f"nodes must be strictly increasing in time; found {previous.time.isoformat()} "
                f">= {current.time.isoformat()}"
            )

    if time < nodes[0].time or time > nodes[-1].time:
        raise InterpolationError(
            f"requested time {time.isoformat()} is outside the interpolated interval "
            f"[{nodes[0].time.isoformat()}, {nodes[-1].time.isoformat()}] — "
            "no extrapolation (.ai/main-prompt.md §1, §11)"
        )

    # Бинарный поиск отрезка [nodes[lo], nodes[hi]], hi = lo + 1, такого что
    # nodes[lo].time <= time <= nodes[hi].time — сетка неравномерна (главный
    # факт про этот источник, см. модульный docstring), поэтому индекс нельзя
    # вычислить по постоянному шагу.
    lo, hi = 0, len(nodes) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if nodes[mid].time <= time:
            lo = mid
        else:
            hi = mid
    node0, node1 = nodes[lo], nodes[hi]

    dt_seconds = (node1.time - node0.time).total_seconds()
    if dt_seconds <= 0:
        raise InterpolationError("degenerate node interval (dt <= 0) after bracket search")
    s = (time - node0.time).total_seconds() / dt_seconds

    x = _interpolate_axis(
        node0.position_km[0], node0.velocity_km_s[0],
        node1.position_km[0], node1.velocity_km_s[0],
        dt_seconds=dt_seconds, s=s,
    )
    y = _interpolate_axis(
        node0.position_km[1], node0.velocity_km_s[1],
        node1.position_km[1], node1.velocity_km_s[1],
        dt_seconds=dt_seconds, s=s,
    )
    z = _interpolate_axis(
        node0.position_km[2], node0.velocity_km_s[2],
        node1.position_km[2], node1.velocity_km_s[2],
        dt_seconds=dt_seconds, s=s,
    )

    return InterpolatedState(
        time=time,
        position_km=(x[0], y[0], z[0]),
        velocity_km_s=(x[1], y[1], z[1]),
    )


__all__ = [
    "COORDINATE_SYSTEM",
    "INTERPOLATION_METHOD",
    "POSITION_UNIT",
    "VELOCITY_UNIT",
    "InterpolatedState",
    "InterpolationError",
    "OemNode",
    "interpolate_state",
]
