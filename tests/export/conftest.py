"""Общие фикстуры для тестов слоя выгрузки (FN-36, S2-06).

Тесты этого пакета намеренно не поднимают ``TestClient``/хранилище там, где
это не нужно: ``src/export`` — чистые функции над уже сохранённым объектом,
поэтому большинство тестов кормят их напрямую четырьмя контрактными
фикстурами (``contracts/fixtures/*.json``) — теми же объектами, которые
``contracts/README.md`` объявляет источником истины о форме результата.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

CONTRACTS_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"

FIXTURE_NAMES = ["success", "incomplete", "equal-windows", "source-error"]


def load_fixture(name: str) -> dict[str, Any]:
    path = CONTRACTS_FIXTURES_DIR / f"{name}.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@pytest.fixture(params=FIXTURE_NAMES)
def contract_fixture(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Один из четырёх сохранённых результатов контракта, свежая копия на тест."""
    return copy.deepcopy(load_fixture(request.param))


@pytest.fixture
def success_result() -> dict[str, Any]:
    return load_fixture("success")
