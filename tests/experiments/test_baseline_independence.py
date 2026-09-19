"""Baseline is honestly independent (FN-43): ``experiments/baseline.py``
must not import ``experiments.production`` or ``experiments.donki_evidence``
— the naive method being compared against must not secretly borrow the
sophisticated window-overlap logic it is defined not to do
(main-prompt.md §11 "без расчёта пересечений"). Checked structurally via
``ast`` (import statements), not just by code review convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

BASELINE_PATH = Path(__file__).resolve().parent.parent.parent / "experiments" / "baseline.py"

_FORBIDDEN_MODULES = {"experiments.production", "experiments.donki_evidence"}


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def test_baseline_module_does_not_import_production_or_donki_evidence() -> None:
    source = BASELINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BASELINE_PATH))
    imported = _imported_module_names(tree)

    def _matches_forbidden(name: str) -> bool:
        return any(
            name == forbidden or name.startswith(forbidden + ".")
            for forbidden in _FORBIDDEN_MODULES
        )

    forbidden_hits = {name for name in imported if _matches_forbidden(name)}
    assert not forbidden_hits, (
        f"experiments/baseline.py imports {forbidden_hits}, but the naive "
        "baseline must stay structurally independent of the production "
        "method and the evidence probe it is compared against "
        "(experiments/baseline.py module docstring)."
    )


def test_baseline_module_only_uses_the_raw_donki_parsing_primitive() -> None:
    """A slightly stronger check: the only ``src.sources`` symbol this
    module may use is the raw DONKI notification dataclass/parser
    (``src.sources.archive_probe``) — it must not reach into
    ``src.domain`` or ``src.api`` either, which would let production-style
    interpretation leak into what is supposed to be a dumb baseline."""
    source = BASELINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BASELINE_PATH))
    imported = _imported_module_names(tree)

    src_imports = {name for name in imported if name.startswith("src.")}
    assert src_imports == {"src.sources", "src.sources.archive_probe"}
