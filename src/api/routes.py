"""Тонкие роутеры S1-07 (.ai/main-prompt.md §8 «роутеры тонкие»).

Каждый обработчик здесь делает три вещи и не больше: разбирает HTTP
(путь/тело/query), вызывает ``src/api/service.py``, оборачивает результат в
ответ. Бизнес-логика (сборка результата, обращение к источникам, хранилищу)
живёт в ``service.py``; форма ответов — в ``schemas.py``. Пути и формы —
``contracts/README.md``, раздел «Endpoint shapes для S1-07».
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from src.api.schemas import (
    ApiErrorBody,
    CalculationRequest,
    Mode,
    ResultListItem,
    SourceStatusResponse,
    TaskCreatedResponse,
    TaskStatusResponse,
)
from src.api.service import ApiError, CalculationError, get_all_source_status, refresh_sources
from src.api.service import run_calculation as _run_calculation
from src.store import connect as connect_store
from src.store import get_result as store_get_result
from src.store import list_results as store_list_results

router = APIRouter(prefix="/api")


def _run_job(task_id: str, payload: CalculationRequest, state: Any) -> None:
    """Выполняется в отдельном потоке (см. :func:`create_calculation`).

    Не поднимает исключения наружу: реестр задач — единственный способ, каким
    вызывающая сторона узнаёт об исходе (.ai/backend-prompt.md §6 «статус
    длительной операции — реестр задач», не пробрасываемое исключение в
    фоновом потоке, которое никто не ждёт).
    """
    state.job_registry.mark_running(task_id)
    try:
        result_id = _run_calculation(
            payload,
            settings=state.settings,
            raw_store=state.raw_store,
            registry=state.source_registry,
        )
    except CalculationError as exc:
        state.job_registry.mark_failed(task_id, code=exc.code, message=exc.message)
    except Exception as exc:  # noqa: BLE001 — последний рубеж изоляции: ошибка в
        # одной фоновой задаче не должна убивать поток исполнителя и не должна
        # остаться незамеченной вызывающей стороной — она сохраняется как
        # понятный статус задачи (приёмка FN-26 «статус ошибки понятен»).
        state.job_registry.mark_failed(task_id, code="internal_error", message=str(exc))
    else:
        state.job_registry.mark_done(task_id, result_id=result_id)


@router.post("/calculations", response_model=TaskCreatedResponse, status_code=202)
async def create_calculation(payload: CalculationRequest, request: Request) -> TaskCreatedResponse:
    state = request.app.state
    task_id = state.job_registry.create()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_job, task_id, payload, state)
    return TaskCreatedResponse(task_id=task_id)


@router.get("/calculations/{task_id}", response_model=TaskStatusResponse)
def get_calculation_status(task_id: str, request: Request) -> TaskStatusResponse:
    job = request.app.state.job_registry.get(task_id)
    if job is None:
        raise ApiError(404, "task_not_found", f"unknown task_id: {task_id!r}")
    error = ApiErrorBody(code=job.error.code, message=job.error.message) if job.error else None
    return TaskStatusResponse(
        task_id=job.task_id, status=job.status, result_id=job.result_id, error=error
    )


@router.get("/results/{result_id}")
def get_result(result_id: str, request: Request) -> dict[str, Any]:
    conn = connect_store(request.app.state.settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    if result is None:
        raise ApiError(404, "result_not_found", f"unknown result_id: {result_id!r}")
    return result


@router.get("/results", response_model=list[ResultListItem])
def list_results(
    request: Request, mode: Mode | None = None, limit: int = 20, offset: int = 0
) -> list[ResultListItem]:
    if not (1 <= limit <= 100):
        raise ApiError(422, "invalid_request", "limit must be between 1 and 100")
    if offset < 0:
        raise ApiError(422, "invalid_request", "offset must be >= 0")

    conn = connect_store(request.app.state.settings.store_db_path)
    try:
        rows = store_list_results(conn, mode=mode, limit=offset + limit)
    finally:
        conn.close()

    page = rows[offset : offset + limit]
    return [
        ResultListItem(
            result_id=row["result_id"],
            computed_at=row["computed_at"],
            mode=row["mode"],
            as_of=row.get("as_of"),
            recommendation_status=row["recommendation"]["status"],
        )
        for row in page
    ]


@router.post("/sources/refresh", response_model=list[SourceStatusResponse])
def refresh_sources_endpoint(request: Request) -> list[SourceStatusResponse]:
    state = request.app.state
    statuses = refresh_sources(
        settings=state.settings, raw_store=state.raw_store, registry=state.source_registry
    )
    return [SourceStatusResponse(**status) for status in statuses]


@router.get("/sources/status", response_model=list[SourceStatusResponse])
def sources_status(request: Request) -> list[SourceStatusResponse]:
    statuses = get_all_source_status(registry=request.app.state.source_registry)
    return [SourceStatusResponse(**status) for status in statuses]


__all__ = ["router"]
