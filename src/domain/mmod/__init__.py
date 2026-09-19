"""Механизм 2: метеороидные потоки, радиант, экранирование, фон.

Геометро-кинематическая часть (экранирование Землёй, преобразование
радианта RA/Dec, относительная скорость встречи, ``effective_flux_ratio``)
реализована в :mod:`src.domain.mmod.geometry` (задача FN-32). Согласованное
масштабирование спорадического фона и итоговый ``ratio_to_background`` не
реализованы — см. ограничения в docstring ``geometry.py`` и
``docs/mechanisms.md`` §10.
"""

from src.domain.mmod.geometry import (
    EARTH_MEAN_RADIUS_KM,
    MmodGeometryAssessment,
    MmodImpossibleInputError,
    MmodInputError,
    MmodMissingInputError,
    Vector3,
    angle_to_nadir_deg,
    assess_shower_geometry,
    effective_flux_ratio,
    is_radiant_shielded,
    radiant_unit_vector_equatorial,
    relative_velocity_kms,
    shielding_critical_angle_deg,
)

__all__ = [
    "EARTH_MEAN_RADIUS_KM",
    "MmodGeometryAssessment",
    "MmodImpossibleInputError",
    "MmodInputError",
    "MmodMissingInputError",
    "Vector3",
    "angle_to_nadir_deg",
    "assess_shower_geometry",
    "effective_flux_ratio",
    "is_radiant_shielded",
    "radiant_unit_vector_equatorial",
    "relative_velocity_kms",
    "shielding_critical_angle_deg",
]
