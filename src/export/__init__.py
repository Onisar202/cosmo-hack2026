"""Слой выгрузки: HTML и JSON из одного сохранённого результата.

Оба формата строятся только из объекта, который уже вернул
``store.get_result`` (``contracts/result.schema.json``) — экспорт не ходит
в сеть, не выбирает записи в хранилище и не пересчитывает домен
(.ai/main-prompt.md §8 «export/ не знает про источники»). Вызывающая
сторона (``src/api/routes.py``) читает результат ровно так же, как
``GET /api/results/{result_id}``, и передаёт этот же объект сюда — так
интерфейс и оба формата выгрузки гарантированно читают один и тот же
сохранённый объект (main-prompt.md §3).
"""

from src.export.errors import ExportError
from src.export.html_export import export_result_html
from src.export.json_export import export_result_json

__all__ = ["ExportError", "export_result_html", "export_result_json"]
