"""Тесты геометро-кинематической части Механизма 2 (MMOD) — задача FN-32.

Три независимых источника проверки, ни один не повторяет собственную
реализацию модуля (.ai/main-prompt.md §9 п.5 «тест, повторяющий собственную
реализацию, доказательством не является»):

1. Эталонные случаи ``tests/fixtures/mmod/reference-cases.json``
   (FN-25/S1-09) — значения экранирования и ``effective_flux_ratio``
   посчитаны отдельным скриптом двумя независимыми методами каждая, здесь
   используются только как ожидаемый результат, без пересчёта.
2. Известные тригонометрические тождества для перевода RA/Dec в декартов
   вектор (RA=0/Dec=0 → ось X и т.п.) — математические факты, не связанные
   с реализацией модуля.
3. Прямые геометрические рассуждения о границе экранирования (строгое
   неравенство "меньше") и об отсутствии повторного учёта скоростной
   поправки (нулевая скорость станции ⇒ поправки нет).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from src.domain.mmod import geometry

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "mmod"


def _load_reference_cases() -> dict:
    return json.loads((FIXTURES_DIR / "reference-cases.json").read_text())


# ---------------------------------------------------------------------------
# 1. Эталонные случаи FN-25 (tests/fixtures/mmod/reference-cases.json)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", ["A_radiant_blocked", "B_radiant_open"])
def test_reference_cases_shielding_and_flux_ratio(case_id: str) -> None:
    """Экранирование и ``effective_flux_ratio`` совпадают с независимо
    вычисленным (двумя методами, вне этого модуля) эталоном FN-25 в
    пределах документированного допуска."""
    data = _load_reference_cases()
    shared = data["shared_inputs"]
    tolerance = data["tolerances"]["velocity_kms"]
    case = next(c for c in data["cases"] if c["case_id"] == case_id)

    radiant_unit_vector = tuple(case["input"]["radiant_unit_vector"])
    station_position_km = tuple(shared["r_station_vector_km"])
    station_velocity_kms = tuple(shared["v_station_vector_kms"])
    v_g = shared["shower_geocentric_velocity_kms"]

    assert geometry.is_radiant_shielded(radiant_unit_vector, station_position_km) is case[
        "expected"
    ]["shielded"]

    ratio = geometry.effective_flux_ratio(
        v_g, radiant_unit_vector, station_position_km, station_velocity_kms
    )
    assert ratio == pytest.approx(case["expected"]["effective_flux_ratio"], abs=tolerance)

    if not case["expected"]["shielded"]:
        v_rel = geometry.relative_velocity_kms(v_g, radiant_unit_vector, station_velocity_kms)
        assert v_rel == pytest.approx(case["expected"]["v_rel_mag_kms"], abs=tolerance)


def test_reference_case_theta_crit_matches_fixture() -> None:
    """``theta_crit`` совпадает с независимо перепроверенным (двумя
    методами вычисления одной геометрической величины) значением фикстуры."""
    data = _load_reference_cases()
    shared = data["shared_inputs"]
    r_station = math.sqrt(sum(c * c for c in shared["r_station_vector_km"]))

    theta_crit = geometry.shielding_critical_angle_deg(r_station)

    assert theta_crit == pytest.approx(
        data["shielding_formula"]["theta_crit_deg"], abs=data["tolerances"]["angle_deg"]
    )


# ---------------------------------------------------------------------------
# 2. RA/Dec → вектор: тригонометрические тождества, независимые от модуля
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ra_deg", "dec_deg", "expected"),
    [
        (0.0, 0.0, (1.0, 0.0, 0.0)),
        (90.0, 0.0, (0.0, 1.0, 0.0)),
        (180.0, 0.0, (-1.0, 0.0, 0.0)),
        (270.0, 0.0, (0.0, -1.0, 0.0)),
        (0.0, 90.0, (0.0, 0.0, 1.0)),
        (0.0, -90.0, (0.0, 0.0, -1.0)),
    ],
)
def test_radiant_unit_vector_matches_known_identities(
    ra_deg: float, dec_deg: float, expected: tuple[float, float, float]
) -> None:
    vector = geometry.radiant_unit_vector_equatorial(ra_deg, dec_deg)
    for actual, ref in zip(vector, expected, strict=True):
        assert actual == pytest.approx(ref, abs=1e-12)


def test_radiant_unit_vector_is_always_unit_length() -> None:
    vector = geometry.radiant_unit_vector_equatorial(337.7, -1.0)
    magnitude = math.sqrt(sum(c * c for c in vector))
    assert magnitude == pytest.approx(1.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 3. Граница экранирования и отсутствие повторного учёта поправки
# ---------------------------------------------------------------------------


def test_shielding_boundary_is_open_at_exact_theta_crit() -> None:
    """Формула — строгое "меньше" (.ai/main-prompt.md §11): угол ровно
    ``theta_crit`` — открытый (касательный) радиант, не экранированный."""
    r_station_km = 6771.0
    theta_crit = geometry.shielding_critical_angle_deg(r_station_km)
    # Радиант в плоскости XZ ровно на угле theta_crit от направления в надир
    # (направление в надир для станции на оси X — вектор (-1, 0, 0)).
    radiant_at_boundary = (
        -math.cos(math.radians(theta_crit)),
        0.0,
        math.sin(math.radians(theta_crit)),
    )
    assert geometry.is_radiant_shielded(radiant_at_boundary, (r_station_km, 0.0, 0.0)) is False


def test_shielding_boundary_is_closed_just_inside_theta_crit() -> None:
    """Пограничный случай воспроизводим с допуском (приёмка FN-32 п.5):
    угол на малую (в пределах допуска) величину меньше ``theta_crit`` —
    экранирован."""
    r_station_km = 6771.0
    theta_crit = geometry.shielding_critical_angle_deg(r_station_km)
    angle = theta_crit - 1e-3
    radiant_just_inside = (
        -math.cos(math.radians(angle)),
        0.0,
        math.sin(math.radians(angle)),
    )
    assert geometry.is_radiant_shielded(radiant_just_inside, (r_station_km, 0.0, 0.0)) is True


def test_no_double_counting_of_relative_velocity_correction() -> None:
    """Приёмка FN-32 п.6: скоростная поправка применяется ровно один раз.
    При нулевой скорости станции относительная скорость встречи равна
    номинальной геоцентрической скорости потока — ``effective_flux_ratio``
    ровно 1.0, без какой-либо дополнительной (повторной) поправки."""
    radiant_unit_vector = (0.173648177667, 0.984807753012, 0.0)  # открытый радиант (case B)
    station_position_km = (6771.0, 0.0, 0.0)
    zero_station_velocity = (0.0, 0.0, 0.0)

    ratio = geometry.effective_flux_ratio(
        66.0, radiant_unit_vector, station_position_km, zero_station_velocity
    )

    assert ratio == pytest.approx(1.0, abs=1e-12)


def test_no_double_counting_matches_manual_dot_product_expectation() -> None:
    """Второй, независимый от ``effective_flux_ratio`` путь проверки того
    же факта: скорость встречи при нулевой скорости станции по построению
    равна |V_g * (-radiant_unit)| = V_g, для любого направления радианта."""
    v_g = 66.0
    for radiant_unit_vector in [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.173648177667, 0.984807753012, 0.0),
    ]:
        v_rel = geometry.relative_velocity_kms(v_g, radiant_unit_vector, (0.0, 0.0, 0.0))
        assert v_rel == pytest.approx(v_g, abs=1e-9)


# ---------------------------------------------------------------------------
# Пропущенные и невозможные параметры — impossible/missing, не 0
# (приёмка FN-32 п.4)
# ---------------------------------------------------------------------------


def test_missing_radiant_ra_raises_not_defaults_to_zero() -> None:
    with pytest.raises(geometry.MmodMissingInputError):
        geometry.radiant_unit_vector_equatorial(None, -1.0)


def test_missing_radiant_dec_raises_not_defaults_to_zero() -> None:
    with pytest.raises(geometry.MmodMissingInputError):
        geometry.radiant_unit_vector_equatorial(337.7, None)


def test_impossible_declination_out_of_range_raises() -> None:
    with pytest.raises(geometry.MmodImpossibleInputError):
        geometry.radiant_unit_vector_equatorial(0.0, 91.0)


def test_missing_shower_velocity_raises_for_open_radiant() -> None:
    radiant_unit_vector = (0.173648177667, 0.984807753012, 0.0)
    with pytest.raises(geometry.MmodMissingInputError):
        geometry.effective_flux_ratio(
            None, radiant_unit_vector, (6771.0, 0.0, 0.0), (0.0, 7.6726, 0.0)
        )


def test_missing_station_position_raises_in_full_assessment() -> None:
    with pytest.raises(geometry.MmodMissingInputError):
        geometry.assess_shower_geometry(
            radiant_ra_deg=337.7,
            radiant_dec_deg=-1.0,
            shower_geocentric_velocity_kms=66.0,
            station_position_km=None,
            station_velocity_kms=(0.0, 7.6726, 0.0),
        )


def test_impossible_station_inside_earth_raises() -> None:
    with pytest.raises(geometry.MmodImpossibleInputError):
        geometry.shielding_critical_angle_deg(geometry.EARTH_MEAN_RADIUS_KM)
    with pytest.raises(geometry.MmodImpossibleInputError):
        geometry.shielding_critical_angle_deg(geometry.EARTH_MEAN_RADIUS_KM - 1.0)


def test_impossible_zero_length_position_vector_raises() -> None:
    with pytest.raises(geometry.MmodImpossibleInputError):
        geometry.angle_to_nadir_deg((1.0, 0.0, 0.0), (0.0, 0.0, 0.0))


def test_shielded_case_reports_v_rel_as_none_not_zero() -> None:
    """Экранированный случай — структурно недостижимое состояние (§8
    reference-cases.json), а не измеренная скорость встречи 0 км/с:
    ``v_rel_kms`` в этом случае ``None`` (не определено), тогда как
    ``effective_flux_ratio`` — определённый по построению 0.0."""
    assessment = geometry.assess_shower_geometry(
        radiant_ra_deg=180.0,  # unit vector (-1, 0, 0) — совпадает с направлением в надир
        radiant_dec_deg=0.0,
        shower_geocentric_velocity_kms=66.0,
        station_position_km=(6771.0, 0.0, 0.0),
        station_velocity_kms=(0.0, 7.6726, 0.0),
    )
    assert assessment.shielded is True
    assert assessment.effective_flux_ratio == 0.0
    assert assessment.v_rel_kms is None


# ---------------------------------------------------------------------------
# Интеграционная проверка полного пути RA/Dec → assess_shower_geometry с
# документированными параметрами эта-Аквариид (sources.yaml)
# ---------------------------------------------------------------------------


def test_full_assessment_eta_aquariids_open_radiant_is_plausible() -> None:
    """Радиант эта-Аквариид (sources.yaml#imo-shower-calendar-eta-aquariids)
    в направлении, заведомо открытом от станции (радиант почти напротив
    надира — direct dot product до вызова модуля даёт угол ~112° > theta_crit
    ~70.2°), даёт конечный положительный ``effective_flux_ratio`` без
    исключений — не точное число (это не эталонный случай FN-25 с
    известным ответом), а структурная проверка, что публичный путь
    работает end-to-end."""
    assessment = geometry.assess_shower_geometry(
        radiant_ra_deg=337.7,
        radiant_dec_deg=-1.0,
        shower_geocentric_velocity_kms=66.0,
        station_position_km=(0.0, -6771.0, 0.0),
        station_velocity_kms=(7.6726, 0.0, 0.0),
    )
    assert assessment.shielded is False
    assert assessment.effective_flux_ratio > 0.0
    assert math.isfinite(assessment.effective_flux_ratio)
