"""SQLite-схема неизменяемого хранилища записей источников и результатов.

Хранилище только дополняется (.ai/backend-prompt.md §1–2): эта схема не
объявляет ни одной операции ``UPDATE``/``DELETE``, и публичный интерфейс
``store/`` (``records.py``, ``results.py``) их тоже не предоставляет —
единственные операции над сохранёнными данными — вставка и чтение.

Хранилище живёт в подключаемом томе (путь передаётся вызывающей стороной,
см. ``src/config.py``), а не во временной директории контейнера: перезапуск
сервиса не должен терять расчёты (.ai/backend-prompt.md §2).

Срок хранения и лицензии: записи и результаты хранятся бессрочно —
автоматического TTL или фоновой очистки нет (main-prompt §2, §3: поздние
уточнения и дубли лежат рядом со старыми версиями, ничего не вытесняется по
времени). Условия лицензии конкретного источника (допустимость локального
хранения оригинала, требования к атрибуции, ограничения на срок хранения)
фиксируются в ``sources.yaml`` при добавлении соответствующего коннектора
(.ai/main-prompt.md §7, §10); на этом этапе коннекторов ещё нет — хранилище
само по себе не решает, какие источники разрешено хранить, оно лишь не
удаляет и не перезаписывает то, что было записано согласно этой политике.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS source_records (
    record_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    provider_record_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    replay_eligible INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    raw_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (source_id, provider_record_id, source_version)
);

CREATE INDEX IF NOT EXISTS idx_source_records_as_of
    ON source_records (source_id, provider_record_id, published_at);

CREATE INDEX IF NOT EXISTS idx_source_records_observed_at
    ON source_records (source_id, observed_at);

CREATE TABLE IF NOT EXISTS calculation_results (
    result_id TEXT PRIMARY KEY,
    computed_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    as_of TEXT,
    algorithm_version TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calculation_results_mode
    ON calculation_results (mode, computed_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Открывает файл SQLite (создавая его и родительский каталог при
    необходимости) и применяет схему.

    Идемпотентна: повторный вызов на существующем файле не пересоздаёт и не
    очищает таблицы (``CREATE TABLE IF NOT EXISTS``), что нужно как для
    перезапуска сервиса, так и для повторного открытия в тестах.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_DDL)
    conn.commit()
    return conn
