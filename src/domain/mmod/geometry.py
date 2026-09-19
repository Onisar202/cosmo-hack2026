"""Геометрия и кинематика Механизма 2 (MMOD): экранирование Землёй,
преобразование радианта RA/Dec в систему координат станции, относительная
скорость встречи, ``effective_flux_ratio``.

Чистый расчётный модуль (.ai/main-prompt.md §8): принимает геометрию станции
(положение/скорость) и параметры потока явными аргументами, не ходит в сеть,
не читает "сейчас" и не пересчитывает орбиту сам — распространение орбиты
даёт ``src/domain/orbit``. Формулы и обоснование — ``docs/mechanisms.md``
§5-§9; эталонные значения — ``tests/fixtures/mmod/reference-cases.json``
(задача FN-25/S1-09, исследовательская часть).

Область применимости и известные упрощения (FN-32, задача этого модуля):

- Земля — непрозрачная сфера радиусом ``EARTH_MEAN_RADIUS_KM`` (сферическое
  приближение WGS84, без сплюснутости и полутени); радиант — точечный
  источник в фиксированном направлении (docs/mechanisms.md §5).
- Радиант потока в ``sources.yaml`` задан в геоцентрической экваториальной
  системе (RA/Dec, эпоха J2000/ICRS рабочих списков IMO/IAU MDC). Положение
  станции из ``src/domain/orbit`` — в TEME (True Equator, Mean Equinox
  **даты**). Эта функция преобразует RA/Dec в декартов единичный вектор
  напрямую (без коррекции на прецессию/нутацию между J2000 и истинным
  экватором даты) и использует его как приближение вектора в TEME.
  Накопленная прецессия между эпохой J2000.0 и 2024 г. — порядка
  ``50.3″/год × ~24 года ≈ 1200″ ≈ 0.33°`` (стандартная скорость общей
  прецессии по прямому восхождению, IAU); это на два порядка меньше
  полуугла экранирования (~70.2°, см. ``shielding_critical_angle_deg``) и
  не может изменить результат ``is_radiant_shielded`` иначе как для
  радианта, лежащего в пределах ~0.33° от границы ``theta_crit`` — открытый
  блокер, зафиксированный в ``docs/mechanisms.md`` §10, а не скрытая
  неточность.
- ``effective_flux_ratio`` — геометро-кинематический коэффициент
  масштабирования потока (docs/mechanisms.md §6), НЕ итоговый
  ``ratio_to_background`` и не уровень MMOD (Фон/Повышенный/Выраженный).
  Согласованное масштабирование спорадического фона той же парой эффектов
  (docs/mechanisms.md §7) в этом модуле не реализовано: в ``sources.yaml``
  нет подтверждённого числового значения спорадического фона
  (``sporadic-background-dynamical-model`` явно помечен как нереализованная
  описательная сверка), а сетевой доступ к первичным источникам (NASA
  MEO/MEMR2 NTRS, IMO, CAMS, arXiv) заблокирован прокси этой сессии — то же
  ограничение, что и в FN-25. Пороги 1.2/2 (.ai/main-prompt.md §11) поэтому
  этим модулем не применяются: применить их к ``effective_flux_ratio`` без
  согласованно отмасштабированного фона значило бы придумать смысл
  отношения, который не доказан (задача FN-32, «Правила»).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_MEAN_RADIUS_KM = 6371.0
"""Сферическое приближение радиуса Земли, км.
Источник: sources.yaml#geometry-constants-earth-orbit (WGS84/IERS)."""

_VECTOR_MAGNITUDE_EPS = 1e-9


class MmodInputError(ValueError):
    """Базовый класс ошибок входных данных геометрии MMOD. Пропущенный или
    физически невозможный параметр всегда поднимает исключение — никогда не
    подменяется нулём или иным правдоподобным значением (.ai/main-prompt.md
    §2, задача FN-32 «Пропущенный параметр даёт impossible/missing, не
    0»)."""


class MmodMissingInputError(MmodInputError):
    """Обязательный параметр не передан (``None``) — данных нет, это не то
    же самое, что физически невозможное значение."""


class MmodImpossibleInputError(MmodInputError):
    """Параметр передан, но физически невозможен или не определён
    (например склонение вне [-90, 90], нулевой вектор положения, станция на
    радиусе меньше радиуса Земли)."""


Vector3 = tuple[float, float, float]


def _require_present(value: float | None, name: str) -> float:
    if value is None:
        raise MmodMissingInputError(f"{name} отсутствует (missing) — расчёт невозможен без него")
    if not math.isfinite(value):
        raise MmodImpossibleInputError(f"{name}={value!r} не является конечным числом")
    return value


def _require_finite_vector(vector: Vector3 | None, name: str) -> Vector3:
    if vector is None:
        raise MmodMissingInputError(f"{name} отсутствует (missing) — расчёт невозможен без него")
    if len(vector) != 3 or not all(math.isfinite(c) for c in vector):
        raise MmodImpossibleInputError(
            f"{name}={vector!r} должен быть конечным вектором из 3 компонент"
        )
    return vector


def _vector_magnitude(vector: Vector3) -> float:
    return math.sqrt(sum(c * c for c in vector))


def _unit_vector(vector: Vector3, *, name: str) -> Vector3:
    magnitude = _vector_magnitude(vector)
    if magnitude < _VECTOR_MAGNITUDE_EPS:
        raise MmodImpossibleInputError(
            f"{name}={vector!r} имеет нулевую (или вырожденную) длину — направление не определено"
        )
    return (vector[0] / magnitude, vector[1] / magnitude, vector[2] / magnitude)


def _dot(a: Vector3, b: Vector3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _subtract(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(vector: Vector3, factor: float) -> Vector3:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def radiant_unit_vector_equatorial(
    radiant_ra_deg: float | None, radiant_dec_deg: float | None
) -> Vector3:
    """Единичный вектор направления на радиант в геоцентрической
    экваториальной системе (RA/Dec, sources.yaml), приближённо
    отождествляемой с TEME станции (см. ограничение области применимости в
    docstring модуля).

    ``ra_deg`` — прямое восхождение [0, 360) градусов, ``dec_deg`` —
    склонение [-90, 90] градусов. Оба обязательны: неизвестный радиант не
    может быть молчаливо принят за 0°/0°.
    """
    ra = _require_present(radiant_ra_deg, "radiant_ra_deg")
    dec = _require_present(radiant_dec_deg, "radiant_dec_deg")
    if not -90.0 <= dec <= 90.0:
        raise MmodImpossibleInputError(f"radiant_dec_deg={dec} вне допустимого диапазона [-90, 90]")
    ra_rad = math.radians(ra)
    dec_rad = math.radians(dec)
    return (
        math.cos(dec_rad) * math.cos(ra_rad),
        math.cos(dec_rad) * math.sin(ra_rad),
        math.sin(dec_rad),
    )


def angle_to_nadir_deg(radiant_unit_vector: Vector3, station_position_km: Vector3) -> float:
    """Угол между направлением на радиант и направлением в надир (от
    станции к центру Земли), градусы. Формула — .ai/main-prompt.md §11
    «экранирование Землёй»."""
    radiant_unit_raw = _require_finite_vector(radiant_unit_vector, "radiant_unit_vector")
    radiant_unit = _unit_vector(radiant_unit_raw, name="radiant_unit_vector")
    position = _require_finite_vector(station_position_km, "station_position_km")
    nadir_direction_name = "nadir direction (from station_position_km)"
    nadir_unit = _unit_vector(_scale(position, -1.0), name=nadir_direction_name)
    cos_angle = max(-1.0, min(1.0, _dot(radiant_unit, nadir_unit)))
    return math.degrees(math.acos(cos_angle))


def shielding_critical_angle_deg(r_station_km: float | None) -> float:
    """``theta_crit = arcsin(R_earth / r_station)`` — критический угол
    экранирования, градусы (.ai/main-prompt.md §11)."""
    r_station = _require_present(r_station_km, "r_station_km")
    if r_station <= EARTH_MEAN_RADIUS_KM:
        raise MmodImpossibleInputError(
            f"r_station_km={r_station} не больше EARTH_MEAN_RADIUS_KM={EARTH_MEAN_RADIUS_KM} "
            "— станция не может находиться на или внутри поверхности Земли"
        )
    return math.degrees(math.asin(EARTH_MEAN_RADIUS_KM / r_station))


def is_radiant_shielded(radiant_unit_vector: Vector3, station_position_km: Vector3) -> bool:
    """true, если Земля закрывает линию визирования на радиант: угол до
    надира строго меньше ``theta_crit`` (.ai/main-prompt.md §11 — «меньше»,
    равенство теta_crit — открытый радиант, касательный луч)."""
    position = _require_finite_vector(station_position_km, "station_position_km")
    r_station = _vector_magnitude(position)
    theta_crit = shielding_critical_angle_deg(r_station)
    angle = angle_to_nadir_deg(radiant_unit_vector, station_position_km)
    return angle < theta_crit


def relative_velocity_kms(
    shower_geocentric_velocity_kms: float | None,
    radiant_unit_vector: Vector3,
    station_velocity_kms: Vector3,
) -> float:
    """Модуль скорости встречи метеороида и станции, км/с
    (.ai/main-prompt.md §11 «скорость встречи»):

    ``v_meteor_vec = V_g * (-radiant_unit_vector)`` (метеороид летит от
    радианта к Земле, т.е. противоположно направлению на радиант);
    ``v_rel_vec = v_meteor_vec - v_station_vec``.
    """
    v_g = _require_present(shower_geocentric_velocity_kms, "shower_geocentric_velocity_kms")
    if v_g <= 0.0:
        raise MmodImpossibleInputError(
            f"shower_geocentric_velocity_kms={v_g} должна быть положительной"
        )
    radiant_unit_raw = _require_finite_vector(radiant_unit_vector, "radiant_unit_vector")
    radiant_unit = _unit_vector(radiant_unit_raw, name="radiant_unit_vector")
    station_velocity = _require_finite_vector(station_velocity_kms, "station_velocity_kms")
    v_meteor_vec = _scale(radiant_unit, -v_g)
    v_rel_vec = _subtract(v_meteor_vec, station_velocity)
    return _vector_magnitude(v_rel_vec)


def effective_flux_ratio(
    shower_geocentric_velocity_kms: float | None,
    radiant_unit_vector: Vector3,
    station_position_km: Vector3,
    station_velocity_kms: Vector3,
) -> float:
    """Геометро-кинематический коэффициент масштабирования направленного
    потока (docs/mechanisms.md §6): 0, если радиант закрыт Землёй, иначе
    ``|v_rel_vec| / V_g``.

    **Не** является ``ratio_to_background`` и не несёт готового уровня MMOD
    — см. docstring модуля."""
    if is_radiant_shielded(radiant_unit_vector, station_position_km):
        return 0.0
    v_g = _require_present(shower_geocentric_velocity_kms, "shower_geocentric_velocity_kms")
    v_rel = relative_velocity_kms(v_g, radiant_unit_vector, station_velocity_kms)
    return v_rel / v_g


@dataclass(frozen=True)
class MmodGeometryAssessment:
    """Результат геометро-кинематической оценки радианта одного потока в
    один момент траектории станции. Не содержит ``ratio_to_background`` и
    не классифицирует уровень MMOD — см. docstring модуля."""

    shielded: bool
    angle_to_nadir_deg: float
    theta_crit_deg: float
    effective_flux_ratio: float
    v_rel_kms: float | None
    """``None``, если радиант закрыт: относительная скорость в этом случае
    не определена содержательно (поток структурно не достигает станции), а
    не "равна нулю" — отличие от ``effective_flux_ratio=0.0``, который
    является определением, а не недостающим измерением."""


def assess_shower_geometry(
    *,
    radiant_ra_deg: float | None,
    radiant_dec_deg: float | None,
    shower_geocentric_velocity_kms: float | None,
    station_position_km: Vector3 | None,
    station_velocity_kms: Vector3 | None,
) -> MmodGeometryAssessment:
    """Полная геометро-кинематическая оценка одного потока в один момент
    траектории станции: RA/Dec → вектор радианта → экранирование →
    относительная скорость → ``effective_flux_ratio``.

    Любой отсутствующий (``None``) или физически невозможный параметр
    поднимает :class:`MmodMissingInputError`/:class:`MmodImpossibleInputError`
    — вызывающая сторона (будущая интеграция с ``src/domain/windows``)
    обязана явно отобразить это в ``mechanismStatus`` контракта
    (``missing_data``/``source_error``), а не подставлять правдоподобное
    значение (задача FN-32, приёмка п.4).
    """
    position = _require_finite_vector(station_position_km, "station_position_km")
    velocity = _require_finite_vector(station_velocity_kms, "station_velocity_kms")
    radiant_unit = radiant_unit_vector_equatorial(radiant_ra_deg, radiant_dec_deg)
    r_station = _vector_magnitude(position)
    theta_crit = shielding_critical_angle_deg(r_station)
    angle = angle_to_nadir_deg(radiant_unit, position)
    shielded = angle < theta_crit

    if shielded:
        return MmodGeometryAssessment(
            shielded=True,
            angle_to_nadir_deg=angle,
            theta_crit_deg=theta_crit,
            effective_flux_ratio=0.0,
            v_rel_kms=None,
        )

    v_g = _require_present(shower_geocentric_velocity_kms, "shower_geocentric_velocity_kms")
    v_rel = relative_velocity_kms(v_g, radiant_unit, velocity)
    return MmodGeometryAssessment(
        shielded=False,
        angle_to_nadir_deg=angle,
        theta_crit_deg=theta_crit,
        effective_flux_ratio=v_rel / v_g,
        v_rel_kms=v_rel,
    )
