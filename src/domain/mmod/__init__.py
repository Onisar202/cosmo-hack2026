"""Механизм 2: метеороидные потоки, радиант, экранирование, фон.

Геометро-кинематическая часть (экранирование Землёй, преобразование
радианта RA/Dec, относительная скорость встречи, ``effective_flux_ratio``)
реализована в :mod:`src.domain.mmod.geometry` (задача FN-32) — источник
числителя, не итоговый уровень.

``ratio_to_background`` и пороги 1.2/2 реализованы в
:mod:`src.domain.mmod.background` (задача FN-39, S2-08): числитель/
знаменатель даёт уже согласованно отнормированный первичный источник
(``sources.yaml#nasa-meo-leo-forecast-2024``, NASA MEO), не геометрия этого
пакета — см. docstring ``background.py`` о границе интеграции (эти два
модуля НЕ перемножаются, см. ``src/api/service.py``)."""

from src.domain.mmod.background import (
    INTERPOLATION_VERSION,
    BackgroundNode,
    Level,
    MmodBackgroundAssessment,
    UnsupportedMmodBackgroundRecordError,
    assess_mmod_background,
    background_nodes_from_records,
    classify_level,
    ratio_to_background,
)
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
    "INTERPOLATION_VERSION",
    "BackgroundNode",
    "Level",
    "MmodBackgroundAssessment",
    "MmodGeometryAssessment",
    "MmodImpossibleInputError",
    "MmodInputError",
    "MmodMissingInputError",
    "UnsupportedMmodBackgroundRecordError",
    "Vector3",
    "angle_to_nadir_deg",
    "assess_mmod_background",
    "assess_shower_geometry",
    "background_nodes_from_records",
    "classify_level",
    "effective_flux_ratio",
    "is_radiant_shielded",
    "radiant_unit_vector_equatorial",
    "ratio_to_background",
    "relative_velocity_kms",
    "shielding_critical_angle_deg",
]
