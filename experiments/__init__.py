"""FN-43 (T5): minimal, honest experiment stand.

Compares the production method (real domain/source modules of this repo,
see ``experiments/production.py``) against a naive baseline
(``experiments/baseline.py``) on three scenarios — a well-covered event,
a quiet control period, and a proven archival gap — using only bundled
fixture/static files already committed to this repository (no network).

See the repository ``README.md`` ("Стенд экспериментов Т5 (FN-43)") and
``docs/method.md`` for the full write-up, and ``experiments/run.py`` for the
CLI entry point (``uv run python -m experiments.run``).
"""
