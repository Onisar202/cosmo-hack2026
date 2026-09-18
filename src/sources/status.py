"""Статус источника как отдельная сущность (.ai/backend-prompt.md §3).

«Статус источника — отдельная сущность: время последнего успешного
получения, время последней ошибки, текст ошибки, действует ли заморозка.
Отдаётся API рядом со значениями, а не вместо них.» Поля ``SourceStatus``
намеренно совпадают с ``contracts/result.schema.json`` →
``$defs`` источника статуса (``source_id``, ``last_success_at``,
``last_error_at``, ``last_error_message``, ``frozen``, ``quota_limited``),
чтобы будущий слой API мог отдавать этот объект без переименования полей.

Реализует «переключатель отключения и заморозки источника» из приёмки
FN-22 как два независимых механизма:

- **заморозка** — переключатель в оперативной памяти
  (:meth:`SourceStatusRegistry.freeze`/``unfreeze``), меняется без
  перезапуска сервиса вызовом функции (в будущем — административным
  эндпоинтом); не трогает уже сохранённые записи, лишь останавливает новые
  обращения к источнику.
- **отключение** — документированная статическая настройка
  ``enabled: false`` в ``sources.yaml`` (секция конкретного источника),
  читается заново при каждом обращении (не кешируется вечно), поэтому
  изменение файла тоже действует без перезапуска.

Оба состояния снаружи видны через одно и то же поле контракта ``frozen``
(:func:`effective_status`) — контракт не различает причину, почему источник
сейчас не обновляется, различает только код (main-prompt.md §7).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone


@dataclass(frozen=True)
class SourceStatus:
    """Форма, совпадающая с ``result.schema.json`` → ``source_status[*]``."""

    source_id: str
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_message: str | None
    frozen: bool
    quota_limited: bool


def _empty_status(source_id: str) -> SourceStatus:
    return SourceStatus(
        source_id=source_id,
        last_success_at=None,
        last_error_at=None,
        last_error_message=None,
        frozen=False,
        quota_limited=False,
    )


class SourceStatusRegistry:
    """Потокобезопасный реестр статусов источников в оперативной памяти.

    Хранит только *наблюдаемость* (время/текст последнего успеха и ошибки,
    заморозку) — не сами данные. Инъецируется явным параметром в
    ``fetch_and_store`` (а не читается как скрытый модульный синглтон),
    чтобы тесты могли использовать свой изолированный экземпляр
    (.ai/backend-prompt.md §2 «никакого состояния расчёта в модульных
    переменных»).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_source: dict[str, SourceStatus] = {}
        # Момент последнего наблюдения (fetched_at последней успешной
        # выборки), отдельно от status.last_success_at (то же значение) —
        # хранится явно, чтобы staleness_seconds не пересчитывал его из
        # публичного объекта, форма которого зафиксирована контрактом.

    def get(self, source_id: str) -> SourceStatus:
        with self._lock:
            return self._by_source.get(source_id, _empty_status(source_id))

    def record_success(self, source_id: str, *, at: datetime) -> SourceStatus:
        """Обновляет только ``last_success_at``.

        ``last_error_at``/``last_error_message``/``quota_limited`` не
        стираются успехом: последняя ошибка остаётся видна в статусе даже
        после восстановления источника (полезно для разбора инцидентов),
        обновляется только при следующей ошибке (см. :meth:`record_error`).
        """
        with self._lock:
            current = self._by_source.get(source_id, _empty_status(source_id))
            updated = replace(current, last_success_at=at)
            self._by_source[source_id] = updated
            return updated

    def record_error(
        self, source_id: str, *, at: datetime, message: str, quota_limited: bool
    ) -> SourceStatus:
        """Обновляет ``last_error_at``/``last_error_message``/``quota_limited`` вместе.

        Три поля выставляются одним вызовом, потому что описывают одну и ту
        же последнюю ошибку: обновлять их порознь позволило бы
        ``quota_limited`` отстать от фактической причины последнего отказа.
        """
        with self._lock:
            current = self._by_source.get(source_id, _empty_status(source_id))
            updated = replace(
                current,
                last_error_at=at,
                last_error_message=message,
                quota_limited=quota_limited,
            )
            self._by_source[source_id] = updated
            return updated

    def freeze(self, source_id: str) -> SourceStatus:
        with self._lock:
            current = self._by_source.get(source_id, _empty_status(source_id))
            updated = replace(current, frozen=True)
            self._by_source[source_id] = updated
            return updated

    def unfreeze(self, source_id: str) -> SourceStatus:
        with self._lock:
            current = self._by_source.get(source_id, _empty_status(source_id))
            updated = replace(current, frozen=False)
            self._by_source[source_id] = updated
            return updated


def effective_status(status: SourceStatus, *, config_enabled: bool) -> SourceStatus:
    """Объединяет оперативную заморозку и статическое отключение в поле ``frozen``.

    Контракт (``result.schema.json``) несёт одно булево поле ``frozen`` —
    снаружи не важно, остановлен ли источник переключателем в памяти или
    ``enabled: false`` в ``sources.yaml``, важно только то, что сейчас
    обновлений не будет.
    """
    if config_enabled:
        return status
    return replace(status, frozen=True)


def staleness_seconds(status: SourceStatus, *, now: datetime) -> float | None:
    """Возраст последнего успешного получения в секундах, либо ``None``.

    ``None`` означает «успешных получений ещё не было» — это не то же самое,
    что «ноль секунд устаревания» (main-prompt.md §2: пропуск не заменяется
    нулём).
    """
    if status.last_success_at is None:
        return None
    return (now - status.last_success_at).total_seconds()


def is_critically_stale(
    status: SourceStatus, *, now: datetime, critical_staleness_seconds: float
) -> bool:
    """``True``, если последний успех либо отсутствует, либо старше порога.

    Отсутствие успешного получения вообще — тоже критическое устаревание
    (main-prompt.md §2: недоступность источника не должна читаться как
    «фон/спокойно» только потому, что формально «данных о просрочке нет»).
    """
    age = staleness_seconds(status, now=now)
    if age is None:
        return True
    return age >= critical_staleness_seconds


def utcnow() -> datetime:
    """Единая точка чтения текущего момента для этого модуля (main-prompt.md §1)."""
    return datetime.now(timezone.utc)
