"""Траектория МКС: распространение орбиты по SGP4.

Чистый расчётный модуль (.ai/backend-prompt.md §7): принимает уже
разобранные орбитальные элементы и явную сетку моментов времени
аргументами. Не читает "сейчас", не ходит в сеть, не обращается к
хранилищу — иначе исторический режим невоспроизводим и нетестируем
(.ai/main-prompt.md §8). Получение и нормализация элементов —
``src/sources/orbit.py``; этот модуль их не знает.

Система координат — TEME (True Equator, Mean Equinox) — собственный выход
SGP4 (Vallado, Crawford, Hujsak, Kelso, "Revisiting Spacetrack Report #3",
2006). Единицы: километры для положения, километры в секунду для
скорости — те же, что отдаёт SGP4, без скрытого преобразования
(.ai/main-prompt.md §2 «единицы хранятся рядом со значением»).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sgp4.api import Satrec, jday

COORDINATE_SYSTEM = "TEME"
POSITION_UNIT = "km"
VELOCITY_UNIT = "km/s"

_UNIX_EPOCH_JD = 2440587.5


class PropagationError(RuntimeError):
    """SGP4 вернул код ошибки для запрошенного момента (например точка
    распада или физически некорректные элементы). Ошибка распространяется
    вызывающей стороне как статус, а не подменяется правдоподобным, но
    невычисленным значением (.ai/main-prompt.md §2)."""


@dataclass(frozen=True)
class OrbitalElements:
    """Орбитальные элементы одного набора GP/TLE, уже разобранные и
    провалидированные слоем ``sources`` (см. ``src/sources/orbit.py``)."""

    line1: str
    line2: str
    epoch: datetime  # UTC — эпоха элементов
    norad_id: str
    coordinate_system: str = COORDINATE_SYSTEM


@dataclass(frozen=True)
class StateVector:
    """Положение/скорость станции в момент ``time`` (TEME, км и км/с)."""

    time: datetime  # UTC
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]
    coordinate_system: str = COORDINATE_SYSTEM


def _require_aware_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be a timezone-aware UTC datetime (.ai/main-prompt.md §1)"
        )


def _julian_to_datetime(jd: float, fr: float) -> datetime:
    """Юлианская дата (+ дробная часть суток) -> timezone-aware UTC datetime.

    ``_UNIX_EPOCH_JD`` — юлианская дата 1970-01-01T00:00:00Z. Проверено на
    официальных тестовых элементах SGP4 (Vallado, satellite 25544, orbit
    606): результат совпадает с независимо опубликованной эпохой
    2020-01-01T19:42:47.134368Z (см. tests/orbit/test_propagation.py).
    """
    days_since_unix_epoch = (jd - _UNIX_EPOCH_JD) + fr
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=days_since_unix_epoch)


def load_elements(line1: str, line2: str, *, norad_id: str) -> OrbitalElements:
    """Разбирает пару строк TLE через SGP4 и извлекает эпоху элементов.

    Чистая функция: только математика над переданными строками, без I/O.
    Целостность строк (контрольная сумма, соответствие NORAD ID) уже
    проверена слоем ``sources`` до вызова этой функции.
    """
    satrec = Satrec.twoline2rv(line1, line2)
    epoch = _julian_to_datetime(satrec.jdsatepoch, satrec.jdsatepochF)
    return OrbitalElements(line1=line1, line2=line2, epoch=epoch, norad_id=norad_id)


def time_grid(
    start: datetime,
    *,
    hours: float,
    step_minutes: float,
    max_hours: float = 32.0,
) -> list[datetime]:
    """Строит сетку моментов от ``start`` до ``start + hours`` включительно,
    с шагом ``step_minutes``.

    Шаг расчёта и горизонт — параметры вызывающей стороны, а не константы
    этого модуля (.ai/main-prompt.md §6 «шаг расчёта траектории — параметр
    конфигурации»). ``max_hours`` — верхняя граница расчётного интервала из
    постановки (32 часа: 24-часовой период поиска начала + 8-часовая ВКД,
    .ai/main-prompt.md §11) — тоже параметр, а не зашитое число, чтобы
    вызывающая сторона могла передать иное значение по своей конфигурации.
    """
    _require_aware_utc(start, "start")
    if hours <= 0:
        raise ValueError(f"hours must be positive, got {hours}")
    if hours > max_hours:
        raise ValueError(
            f"hours={hours} exceeds max_hours={max_hours} (.ai/main-prompt.md §11: "
            "расчётный интервал не превышает 32 часа)"
        )
    if step_minutes <= 0:
        raise ValueError(f"step_minutes must be positive, got {step_minutes}")

    grid: list[datetime] = []
    step = timedelta(minutes=step_minutes)
    end = start + timedelta(hours=hours)
    t = start
    while t < end:
        grid.append(t)
        t += step
    # ``end`` — обязательная точка сетки, а не просто ещё один шаг: когда
    # step_minutes не делит hours нацело (или шаг длиннее самого окна),
    # цикл выше останавливается раньше границы окна и хвост интервала
    # остаётся без расчёта, что может скрыть максимальный уровень
    # воздействия ближе к концу ВКД (round 1 ревью PR #17). Каждый элемент
    # grid всегда строго меньше end (условие цикла), так что это не может
    # задвоить последнюю точку.
    grid.append(end)
    return grid


def propagate(elements: OrbitalElements, times: Sequence[datetime]) -> list[StateVector]:
    """Распространяет орбиту на заданные моменты времени.

    ``times`` — явный аргумент, а не "сейчас": домен не читает часы и не
    ходит в сеть (.ai/main-prompt.md §8). Возвращает положение/скорость в
    той же системе координат и единицах, что и SGP4 (TEME, км, км/с) — без
    скрытого преобразования.
    """
    satrec = Satrec.twoline2rv(elements.line1, elements.line2)
    states: list[StateVector] = []
    for t in times:
        _require_aware_utc(t, "times[]")
        t_utc = t.astimezone(timezone.utc)
        jd, fr = jday(
            t_utc.year,
            t_utc.month,
            t_utc.day,
            t_utc.hour,
            t_utc.minute,
            t_utc.second + t_utc.microsecond / 1e6,
        )
        error_code, position, velocity = satrec.sgp4(jd, fr)
        if error_code != 0:
            raise PropagationError(
                f"SGP4 error code {error_code} at {t_utc.isoformat()} for NORAD "
                f"{elements.norad_id} (epoch {elements.epoch.isoformat()})"
            )
        states.append(
            StateVector(
                time=t,
                position_km=(position[0], position[1], position[2]),
                velocity_km_s=(velocity[0], velocity[1], velocity[2]),
            )
        )
    return states


def elements_age_hours(epoch: datetime, at: datetime) -> float:
    """Давность элементов относительно момента ``at``.

    Отрицательное значение означает, что ``at`` раньше эпохи (элементы "из
    будущего" относительно расчёта) — вызывающая сторона решает, что с этим
    делать; эта функция только считает разницу, ничего не скрывает.
    """
    _require_aware_utc(epoch, "epoch")
    _require_aware_utc(at, "at")
    return (at - epoch).total_seconds() / 3600.0


def is_reconstructed_geometry(
    epoch: datetime, as_of: datetime, *, max_confirmed_age_hours: float
) -> bool:
    """true, если элементы для ``as_of`` не подтверждены и геометрия должна
    быть помечена как реконструкция, отдельно от проверяемого прогноза
    (.ai/main-prompt.md §11 «Траектория»: «Если элементы на момент as_of не
    подтверждены — геометрия помечается как реконструкция»).

    ``max_confirmed_age_hours`` — параметр вызывающей стороны: то, насколько
    давние (или насколько «из будущего» относительно ``as_of``) элементы ещё
    считаются подтверждёнными для конкретного расчёта, — предметное решение
    зоны 2/3, не константа этого модуля.
    """
    return abs(elements_age_hours(epoch, as_of)) > max_confirmed_age_hours
