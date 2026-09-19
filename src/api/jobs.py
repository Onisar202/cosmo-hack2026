"""Реестр задач в процессе (.ai/main-prompt.md §6, .ai/backend-prompt.md §6).

«Не строй инфраструктуру там, где её не нужно» — расчёт (SGP4 на секунды,
сетевые вызовы к источникам на секунды) не оправдывает брокер очередей;
статус длительной операции реализуется реестром задач в памяти процесса.

``JobRegistry`` хранит только статус задачи (``pending``/``running``/``done``/
``failed``), не параметры и не промежуточные данные расчёта — те передаются
явно по вызовам (.ai/backend-prompt.md §2 «никакого состояния расчёта в
модульных переменных: контекст передаётся явно»). Реестр — единственный
разделяемый между конкурентными запросами объект в этом модуле, и он
потокобезопасен: каждая задача живёт в своей записи словаря, конкурентные
запросы с разными ``task_id`` не пересекаются (приёмка FN-26 «изоляция
запросов»).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

JobStatus = Literal["pending", "running", "done", "failed"]


@dataclass(frozen=True)
class JobError:
    code: str
    message: str


@dataclass(frozen=True)
class Job:
    task_id: str
    status: JobStatus
    created_at: datetime
    result_id: str | None = None
    error: JobError | None = None


class UnknownTaskError(KeyError):
    """``task_id`` не найден в реестре (либо никогда не существовал)."""


class JobRegistry:
    """Потокобезопасный реестр задач в оперативной памяти процесса.

    Инъецируется явным параметром (как ``SourceStatusRegistry``), а не
    читается как скрытый модульный синглтон — так тесты изоляции могут
    завести свой экземпляр и утверждать, что конкурентные задачи не делят
    состояние.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def create(self) -> str:
        task_id = str(uuid.uuid4())
        job = Job(task_id=task_id, status="pending", created_at=datetime.now(timezone.utc))
        with self._lock:
            self._jobs[task_id] = job
        return task_id

    def get(self, task_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(task_id)

    def mark_running(self, task_id: str) -> None:
        self._update(task_id, status="running")

    def mark_done(self, task_id: str, *, result_id: str) -> None:
        self._update(task_id, status="done", result_id=result_id, error=None)

    def mark_failed(self, task_id: str, *, code: str, message: str) -> None:
        self._update(task_id, status="failed", result_id=None, error=JobError(code, message))

    def _update(
        self,
        task_id: str,
        *,
        status: JobStatus,
        result_id: str | None = None,
        error: JobError | None = None,
    ) -> None:
        with self._lock:
            current = self._jobs.get(task_id)
            if current is None:
                raise UnknownTaskError(task_id)
            self._jobs[task_id] = replace(
                current, status=status, result_id=result_id, error=error
            )


__all__ = ["Job", "JobError", "JobRegistry", "JobStatus", "UnknownTaskError"]
