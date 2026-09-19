"""JSON-выгрузка из сохранённого результата (FN-36, S2-06, приёмка п.1/3/4/5)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.export import ExportError, export_result_json


def test_export_json_is_semantically_equal_to_the_stored_result(
    contract_fixture: dict[str, Any],
) -> None:
    """Приёмка п.1: JSON export семантически равен GET сохранённого результата.

    ``export_result_json`` не добавляет, не убирает и не переупорядочивает
    поля — на входе объект, который вернул бы ``GET /api/results/{id}``.
    """
    original = copy.deepcopy(contract_fixture)

    exported = export_result_json(contract_fixture)

    assert exported == original


def test_export_json_returns_an_independent_copy(success_result: dict[str, Any]) -> None:
    """Мутация возвращённого объекта не должна «просачиваться» в вызывающую
    сторону/сохранённый результат — иначе повторная выгрузка отдавала бы
    уже испорченные данные (приёмка п.4 «повторный экспорт... не меняет
    данные»)."""
    exported = export_result_json(success_result)

    exported["windows"][0]["window_id"] = "tampered"
    exported["result_id"] = "tampered"

    assert success_result["windows"][0]["window_id"] != "tampered"
    assert success_result["result_id"] != "tampered"


def test_repeated_export_is_deterministic_and_does_not_change_the_result_id(
    success_result: dict[str, Any],
) -> None:
    """Приёмка п.4: повторный экспорт детерминирован и не меняет result_id/данные."""
    first = export_result_json(success_result)
    second = export_result_json(success_result)

    assert first == second
    assert first["result_id"] == second["result_id"] == success_result["result_id"]


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda r: r.pop("windows"), id="missing-required-field"),
        pytest.param(lambda r: r.update(windows=[]), id="windows-below-minItems"),
        pytest.param(
            lambda r: r["windows"][0]["mechanisms"][0].update(status="not-a-real-status"),
            id="invalid-enum-value",
        ),
        pytest.param(
            lambda r: r["windows"][0]["mechanisms"][0].update(
                status="ok", max_level=None
            ),
            id="max_level-null-while-status-ok",
        ),
        pytest.param(
            lambda r: r.update(computed_at="2026-99-99T25:61:61+99:99"),
            id="calendar-invalid-datetime-matches-pattern-but-not-a-real-moment",
        ),
    ],
)
def test_export_json_rejects_a_corrupted_payload_instead_of_a_plausible_report(
    success_result: dict[str, Any], corrupt: Any
) -> None:
    """Приёмка п.3: повреждённый/невалидный payload не даёт правдоподобный отчёт.

    Каждый вариант здесь — повреждение, которое ``store_result`` в норме не
    пропустило бы, но не то, на что можно полагаться при выгрузке: контракт
    может отличаться от того, что фактически лежит в старой строке SQLite.
    ``calendar-invalid-datetime...`` — round 1 ревью PR #26: строка вроде
    ``2026-99-99T25:61:61+99:99`` проходит regex-паттерн ``utcDateTime``
    (цифры на нужных позициях, смещение указано), но не является реальным
    календарным моментом — без проверки ``format: date-time`` она могла бы
    попасть в JSON-выгрузку как корректное время.
    """
    corrupt(success_result)

    with pytest.raises(ExportError):
        export_result_json(success_result)


def test_export_json_rejects_a_non_object_payload() -> None:
    with pytest.raises(ExportError):
        export_result_json([])  # type: ignore[arg-type]
