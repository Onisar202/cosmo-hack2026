"""Машиночитаемый JSON только из уже сохранённого результата (FN-36, S2-06).

``export_result_json`` не перечитывает источники, не пересчитывает домен и
не собирает отдельную версию фактов — единственный вход это уже сохранённый
объект (``contracts/result.schema.json``), тот же, что отдаёт
``GET /api/results/{result_id}`` (main-prompt.md §3 «интерфейс и оба
формата выгрузки читают один и тот же сохранённый объект»). Приёмка FN-36
п.1 требует семантического равенства с этим GET: функция не добавляет, не
убирает и не переупорядочивает поля — только проверяет форму
(``src/export/_validate.py``) и возвращает независимую копию, чтобы
мутация возвращённого объекта вызывающей стороной не могла случайно
«просочиться» назад в сохранённый результат.
"""

from __future__ import annotations

import copy
from typing import Any

from src.export._validate import validate_result_or_raise


def export_result_json(result: dict[str, Any]) -> dict[str, Any]:
    """Возвращает JSON-пригодную копию сохранённого результата ``result``.

    Поднимает :class:`~src.export.errors.ExportError`, если ``result`` не
    проходит ``contracts/result.schema.json``.
    """
    validate_result_or_raise(result)
    return copy.deepcopy(result)


__all__ = ["export_result_json"]
