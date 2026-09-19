"""Проверка сохранённого результата перед выгрузкой (FN-36, S2-06).

Оба формата выгрузки строятся из одного уже сохранённого объекта
(``store.get_result``) и не пересчитывают домен — но именно поэтому обязаны
сами проверить его форму перед рендерингом: приёмка FN-36 п.3 требует, чтобы
повреждённый или невалидный payload не превратился в правдоподобно
выглядящий отчёт. Полагаться на то, что ``store_result``/сервисный слой уже
проверили объект когда-то в прошлом, недостаточно — контракт мог измениться
между сохранением и выгрузкой, а строка в SQLite неизменяема, но сам файл
схемы — нет.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from src.export.errors import ExportError

_CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"

_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_a_real_calendar_moment(value: object) -> bool:
    """Дополняет ``#/$defs/utcDateTime`` в ``result.schema.json``: паттерн
    схемы проверяет только позиции цифр и обязательное смещение, а не то,
    что месяц/день/час — реальные календарные значения. Без этой проверки
    строка вроде ``2026-99-99T25:61:61+99:99`` проходит паттерн и могла бы
    попасть в JSON-/HTML-выгрузку как корректное время (round 1 ревью PR #26).

    ``jsonschema`` без опционального пакета ``rfc3339-validator`` не
    регистрирует проверку ``date-time`` вовсе (``FormatChecker().checkers``
    не содержит ``date-time``) — обычный ``FORMAT_CHECKER`` здесь был бы
    молчаливым no-op, поэтому проверка написана на стандартной библиотеке, не
    добавляя новую зависимость.
    """
    if not isinstance(value, str):
        return True  # тип уже проверяет сама схема (type: string)
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return True


@lru_cache(maxsize=1)
def _result_validator() -> Draft202012Validator:
    schemas = {
        name: json.loads((_CONTRACTS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        for name in ("request", "result")
    }
    registry: Registry[Any] = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return Draft202012Validator(
        schemas["result"], registry=registry, format_checker=_FORMAT_CHECKER
    )


def validate_result_or_raise(result: Any) -> dict[str, Any]:
    """Возвращает ``result``, если он проходит ``contracts/result.schema.json``.

    Поднимает :class:`ExportError` иначе — единственный путь, которым слой
    выгрузки отказывается строить отчёт из данных, не соответствующих
    контракту (не пустой список полей и не «похоже на результат»).
    """
    if not isinstance(result, dict):
        raise ExportError(
            f"stored result must be a JSON object, got {type(result).__name__}: "
            "refusing to export a report from a non-object payload"
        )

    errors = sorted(_result_validator().iter_errors(result), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])
        raise ExportError(
            "stored result does not conform to contracts/result.schema.json, refusing "
            f"to export a report from it: {details}"
        )

    return result


__all__ = ["validate_result_or_raise"]
