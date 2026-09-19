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
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from src.export.errors import ExportError

_CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


@lru_cache(maxsize=1)
def _result_validator() -> Draft202012Validator:
    schemas = {
        name: json.loads((_CONTRACTS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        for name in ("request", "result")
    }
    registry: Registry[Any] = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return Draft202012Validator(schemas["result"], registry=registry)


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
