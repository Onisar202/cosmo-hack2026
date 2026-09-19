"""Слой границы: export не делает сеть/выбор в хранилище/доменные расчёты
(FN-36, приёмка п.5 «нет сети/store selection/domain logic внутри export;
только чтение конкретного сохранённого результата»).

main-prompt.md §8 формулирует эту границу как правило для всего проекта
(«export/ не знает про источники»); здесь она закреплена автоматической
проверкой импортов, а не только соглашением в код-ревью — статический
анализ исходников, а не запуск сети/расчётов (сам этот тест тоже не должен
их использовать).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_EXPORT_DIR = Path(__file__).resolve().parents[2] / "src" / "export"

_FORBIDDEN_MODULE_PREFIXES = (
    "httpx",
    "sqlite3",
    "src.sources",
    "src.domain",
    "src.store",
    "src.api",
)


def _module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def _export_source_files() -> list[Path]:
    return sorted(_EXPORT_DIR.glob("*.py"))


@pytest.mark.parametrize("path", _export_source_files(), ids=lambda p: p.name)
def test_export_module_does_not_import_forbidden_layers(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for module in _module_names(tree):
        for forbidden in _FORBIDDEN_MODULE_PREFIXES:
            assert not (module == forbidden or module.startswith(forbidden + ".")), (
                f"{path.name} imports {module!r}, which belongs to a layer export "
                f"must not depend on (main-prompt.md §8, FN-36 приёмка п.5)"
            )


def test_export_source_files_are_covered_by_this_test() -> None:
    """Не даёт списку забыть новый файл модуля (например при следующей задаче)."""
    names = {p.name for p in _export_source_files()}
    assert names >= {"__init__.py", "errors.py", "json_export.py", "html_export.py"}
