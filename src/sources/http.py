"""Общий HTTP-клиент для коннекторов источников (.ai/backend-prompt.md §3).

Единая точка, где живут правила, обязательные для *каждого* сетевого вызова
слоя ``sources/``: раздельные таймауты на соединение и на чтение, ограниченное
число повторов с экспоненциальной задержкой для временных ошибок и отдельная,
без немедленного повтора, обработка ``429``/квоты (main-prompt.md §5:
«повтор через секунду сделает хуже»). Модуль не знает про конкретный продукт
или формат ответа — это дело коннектора (например ``src/sources/swpc.py``).

Тестируемость: ``fetch`` принимает необязательный ``httpx.Client`` и функцию
``sleep`` — тесты подставляют ``httpx.MockTransport`` (реальных сетевых
вызовов и реального ожидания между повторами в тестах нет, .ai/main-prompt.md
§9 «тесты детерминированы»).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class HttpFetchResult:
    """Результат успешного (2xx) обращения к источнику."""

    status_code: int
    body: bytes
    url: str
    elapsed_seconds: float


class SourceHttpError(RuntimeError):
    """Источник недоступен или ответил ошибкой после исчерпания повторов."""


class SourceTimeoutError(SourceHttpError):
    """Таймаут соединения или чтения (после исчерпания повторов)."""


class SourceQuotaLimitedError(SourceHttpError):
    """Источник ответил 429: квота исчерпана.

    Обрабатывается отдельно от прочих ошибок и никогда не повторяется в
    рамках одного вызова :func:`fetch` (main-prompt.md §5) — решение о том,
    когда пробовать снова, принимает вызывающий слой (периодический refresh
    с TTL, см. ``src/sources/swpc.py``), а не немедленный повтор здесь.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        # Retry-After как HTTP-дата не разбирается здесь: значение — не
        # секунды до повтора, а подсказка человеку в логе; отсутствие
        # разобранного значения не должно ронять обработку 429.
        return None


def fetch(
    url: str,
    *,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    max_retries: int,
    backoff_base_seconds: float,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpFetchResult:
    """Выполняет GET с раздельными таймаутами и ограниченными повторами.

    ``max_retries`` — число повторов *после* первой попытки для временных
    ошибок (сетевая ошибка, таймаут, 5xx); ``429`` и прочие 4xx не
    повторяются. Между повторами — экспоненциальная задержка
    ``backoff_base_seconds * 2**attempt``.
    """
    timeout = httpx.Timeout(
        connect=connect_timeout_seconds,
        read=read_timeout_seconds,
        write=read_timeout_seconds,
        pool=connect_timeout_seconds,
    )
    owns_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=timeout)

    last_error: Exception | None = None
    try:
        for attempt in range(max_retries + 1):
            started = time.monotonic()
            try:
                response = http_client.get(url, timeout=timeout)
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt >= max_retries:
                    raise SourceTimeoutError(
                        f"timeout fetching {url} after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                sleep(backoff_base_seconds * (2**attempt))
                continue
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= max_retries:
                    raise SourceHttpError(
                        f"network error fetching {url} after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                sleep(backoff_base_seconds * (2**attempt))
                continue

            elapsed = time.monotonic() - started

            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                raise SourceQuotaLimitedError(
                    f"{url} responded 429 (quota limited)"
                    + (f", Retry-After={retry_after}s" if retry_after is not None else ""),
                    retry_after_seconds=retry_after,
                )

            if 500 <= response.status_code < 600:
                last_error = SourceHttpError(f"{url} responded {response.status_code}")
                if attempt >= max_retries:
                    raise SourceHttpError(
                        f"{url} responded {response.status_code} after "
                        f"{attempt + 1} attempt(s)"
                    )
                sleep(backoff_base_seconds * (2**attempt))
                continue

            if response.status_code >= 400:
                # Прочие 4xx — не временная ошибка, повтор её не исправит.
                raise SourceHttpError(f"{url} responded {response.status_code} (not retried)")

            return HttpFetchResult(
                status_code=response.status_code,
                body=response.content,
                url=url,
                elapsed_seconds=elapsed,
            )
    finally:
        if owns_client:
            http_client.close()

    # Недостижимо: цикл либо возвращает, либо поднимает исключение на
    # последней попытке; оставлено как явная защита от тихого None.
    raise SourceHttpError(f"failed to fetch {url}: {last_error}")
