"""Читаемый HTML только из уже сохранённого результата (FN-36, S2-06).

Зеркалит те же данные и ту же терминологию, что ``web/src/components``
(``labels.ts``, ``NullValue.tsx``, ``TimeValue.tsx``) — интерфейс и оба
формата выгрузки читают один и тот же сохранённый объект и не должны
расходиться в том, что видит аналитик (main-prompt.md §3, §12 «выгрузка
совпадает с интерфейсом»). Как и интерфейс, этот модуль не придумывает
собственных формулировок об исходе («безопасно», «риска нет») — текст
предупреждений, пояснений механизмов и рекомендации выводится ровно так,
как он хранится в результате; здесь только подписи для фиксированных кодов
контракта и обязательная причина у каждого ``null``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any

from src.export._validate import validate_result_or_raise
from src.export.errors import ExportError

_MODE_LABELS: dict[str, str] = {
    "current": "Текущий расчёт",
    "historical_analysis": "Исторический разбор",
    "historical_forecast": "Строгий прогноз из прошлого",
}

_MECHANISM_LABELS: dict[str, str] = {
    "space_weather": "Космическая погода (поток протонов)",
    "mmod": "MMOD (метеороидная составляющая)",
}

_MECHANISM_STATUS_LABELS: dict[str, str] = {
    "ok": "оценка выполнена",
    "not_implemented": "механизм ещё не реализован",
    "missing_data": "данных нет",
    "stale_data": "данные устарели",
    "source_error": "отказ источника",
    "beyond_horizon": "за горизонтом прогноза",
}

_MECHANISM_STATUS_NULL_REASONS: dict[str, str] = {
    "not_implemented": "механизм ещё не реализован в этой версии сервиса",
    "missing_data": "источник не вернул данные за интервал окна",
    "stale_data": "доступные данные устарели",
    "source_error": "отказ или квота источника",
    "beyond_horizon": "интервал за пределами горизонта прогноза — «не покрыто», не «спокойно»",
}
_DEFAULT_NULL_REASON = "нет данных для оценки"

_WARNING_SEVERITY_LABELS: dict[str, str] = {
    "info": "информация",
    "advisory": "предупреждение",
    "critical": "критично",
}

_LIGHTING_STATUS_LABELS: dict[str, str] = {
    "not_requested": "освещённость не задавалась",
    "satisfied": "условие освещённости выполнено",
    "violated": "условие освещённости нарушено",
}

_RECOMMENDATION_STATUS_LABELS: dict[str, str] = {
    "selected": "Окно выбрано",
    "tie": "Окна равнозначны",
    "insufficient_basis": "Оснований для рекомендации недостаточно",
    "all_windows_excluded": "Все окна исключены из сравнения",
}

_RECORD_KIND_LABELS: dict[str, str] = {
    "observation": "наблюдение",
    "forecast": "внешний прогноз",
    "warning": "предупреждение источника",
    "orbital_elements": "орбитальные элементы",
}

_ORBIT_SOURCE_LABELS: dict[str, str] = {
    "celestrak": "CelesTrak (текущие элементы)",
    "space-track": "Space-Track GP_HISTORY (исторические элементы)",
}

_MECHANISM_UNITS_NOTE = (
    "Единицы: у механизма «космическая погода» пороги S1/S2/S3 — шкала NOAA S по "
    "потоку протонов ≥10 МэВ (pfu); у механизма «MMOD» пороги elevated/pronounced "
    "— безразмерное отношение потока метеороидов к спорадическому фону "
    "(main-prompt.md §11)."
)


def _esc(value: object) -> str:
    return escape(str(value))


def _format_utc(value: str, *, field: str) -> str:
    """Разбирает ISO 8601 время результата и печатает его явно как UTC.

    Не доверяет тому, что строка уже содержит суффикс ``Z``: если смещение
    отсутствует вовсе (naive datetime добрался до сохранённого результата),
    это повреждённый payload, а не время без часового пояса (main-prompt.md
    §1) — экспорт отказывает, а не молча печатает то, что пришло.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportError(f"{field}: not a valid ISO 8601 datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExportError(f"{field}: datetime without an explicit UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _null_span(reason: str) -> str:
    return f'<span class="null-value" title="{_esc(reason)}">нет данных ({_esc(reason)})</span>'


def _optional_utc(value: str | None, *, field: str, reason: str) -> str:
    if value is None:
        return _null_span(reason)
    return _format_utc(value, field=field)


def _row(label: str, value_html: str) -> str:
    return f"<tr><th>{_esc(label)}</th><td>{value_html}</td></tr>"


def _list_or_none(items: list[str], *, empty_reason: str) -> str:
    if not items:
        return _null_span(empty_reason)
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in items) + "</ul>"


def _record_ids_html(record_ids: list[str]) -> str:
    if not record_ids:
        return _null_span("нет ссылки на исходную запись")
    return ", ".join(f"<code>{_esc(rid)}</code>" for rid in record_ids)


def _request_section(result: dict[str, Any]) -> str:
    request = result["request"]
    mode = result["mode"]
    lighting = request.get("lighting_constraint")
    lighting_html = (
        "требуется солнечное освещение"
        if lighting is not None and lighting.get("requires_sunlight")
        else (
            "не требуется солнечное освещение"
            if lighting is not None
            else _null_span("ограничение по освещённости не задавалось в запросе")
        )
    )
    start_at_html = _format_utc(request["start_at"], field="request.start_at")
    rows = [
        _row("Режим", _esc(_MODE_LABELS.get(mode, mode))),
        _row("Начало окна поиска (start_at)", start_at_html),
        _row("Длительность ВКД", f"{_esc(request['duration_hours'])} ч"),
        _row("Период поиска начала", f"{_esc(request['search_window_hours'])} ч"),
        _row(
            "Отсечение as_of",
            _optional_utc(
                result["as_of"],
                field="as_of",
                reason=f"не применяется для режима {_MODE_LABELS.get(mode, mode)!r}",
            ),
        ),
        _row("Ограничение по освещённости", lighting_html),
    ]
    return "<section id=\"request\"><h2>Запрос</h2><table>" + "".join(rows) + "</table></section>"


def _orbit_section(result: dict[str, Any]) -> str:
    orbit = result["orbit"]
    reconstruction_html = (
        "<strong>реконструкция</strong> — элементы на момент as_of/расчёта не подтверждены"
        if orbit["is_reconstructed"]
        else "элементы подтверждены (не реконструкция)"
    )
    rows = [
        _row("Источник", _esc(_ORBIT_SOURCE_LABELS.get(orbit["source"], orbit["source"]))),
        _row("NORAD ID", _esc(orbit["norad_id"])),
        _row("Эпоха элементов", _format_utc(orbit["elements_epoch"], field="orbit.elements_epoch")),
        _row("Давность элементов", f"{_esc(orbit['elements_age_hours'])} ч"),
        _row("Система координат", _esc(orbit["coordinate_system"])),
        _row("Геометрия", reconstruction_html),
        _row("record_id", f"<code>{_esc(orbit['record_id'])}</code>"),
    ]
    return "<section id=\"orbit\"><h2>Орбита</h2><table>" + "".join(rows) + "</table></section>"


def _mechanism_html(mechanism: dict[str, Any]) -> str:
    status = mechanism["status"]
    reason = _MECHANISM_STATUS_NULL_REASONS.get(status, _DEFAULT_NULL_REASON)
    max_level_html = (
        _esc(mechanism["max_level"]) if mechanism["max_level"] is not None else _null_span(reason)
    )
    if mechanism["exceedance_hours_by_level"] is not None:
        exceedance_html = ", ".join(
            f"{_esc(level)}: {_esc(hours)} ч"
            for level, hours in mechanism["exceedance_hours_by_level"].items()
        )
    else:
        exceedance_html = _null_span(reason)

    mechanism_kind = mechanism["mechanism"]
    rows = [
        _row("Механизм", _esc(_MECHANISM_LABELS.get(mechanism_kind, mechanism_kind))),
        _row("Статус", _esc(_MECHANISM_STATUS_LABELS.get(status, status))),
        _row("Максимальный уровень в окне", max_level_html),
        _row("Длительность превышения по порогам", exceedance_html),
        _row("Полнота данных (coverage_fraction)", f"{mechanism['coverage_fraction'] * 100:.0f}%"),
        _row(
            "Критический пробел данных",
            "да" if mechanism["critical_gap"] else "нет",
        ),
        _row("Пояснения", _list_or_none(mechanism["notes"], empty_reason="пояснений нет")),
        _row("Использованные записи (record_id)", _record_ids_html(mechanism["record_ids"])),
    ]
    return "<table class=\"mechanism\">" + "".join(rows) + "</table>"


def _window_html(window: dict[str, Any]) -> str:
    lighting = window["lighting"]
    lighting_status_html = _esc(
        _LIGHTING_STATUS_LABELS.get(lighting["status"], lighting["status"])
    )
    if lighting.get("note"):
        lighting_status_html += f" — {_esc(lighting['note'])}"

    exclusion_html = (
        f'<p class="exclusion">Исключено из сравнения: {_esc(window["exclusion_reason"])}</p>'
        if window["excluded_from_comparison"]
        else "<p>Не исключено из сравнения.</p>"
    )

    mechanisms_html = "".join(_mechanism_html(m) for m in window["mechanisms"])

    return (
        f'<article class="window" id="window-{_esc(window["window_id"])}">'
        f'<h3>Окно {_esc(window["window_id"])}</h3>'
        "<table>"
        + _row("Начало", _format_utc(window["start_at"], field="window.start_at"))
        + _row("Окончание", _format_utc(window["end_at"], field="window.end_at"))
        + _row("Длительность", f"{_esc(window['duration_hours'])} ч")
        + _row("Освещённость", lighting_status_html)
        + "</table>"
        + exclusion_html
        + mechanisms_html
        + "</article>"
    )


def _windows_section(result: dict[str, Any]) -> str:
    windows_html = "".join(_window_html(w) for w in result["windows"])
    return (
        '<section id="windows"><h2>Сравниваемые окна</h2>'
        f'<p class="note">{_MECHANISM_UNITS_NOTE}</p>' + windows_html + "</section>"
    )


def _coverage_section(result: dict[str, Any]) -> str:
    coverage = result["coverage"]
    rows = [
        _row(
            "Запрошенный период поддержан архивом",
            "да" if coverage["requested_period_supported"] else "нет",
        ),
    ]
    if coverage["archive_gaps"]:
        gaps_html = "<ul>" + "".join(
            f"<li><code>{_esc(gap['source_id'])}</code>: "
            f"{_format_utc(gap['gap_start'], field='coverage.archive_gaps.gap_start')} — "
            f"{_format_utc(gap['gap_end'], field='coverage.archive_gaps.gap_end')} — "
            f"{_esc(gap['note'])}</li>"
            for gap in coverage["archive_gaps"]
        ) + "</ul>"
    else:
        gaps_html = _null_span("архивных пробелов не обнаружено")
    rows.append(_row("Архивные пробелы", gaps_html))
    return (
        '<section id="coverage"><h2>Полнота данных</h2><table>'
        + "".join(rows)
        + "</table></section>"
    )


def _recommendation_section(result: dict[str, Any]) -> str:
    recommendation = result["recommendation"]
    status = recommendation["status"]
    window_html = (
        f'<code>{_esc(recommendation["window_id"])}</code>'
        if recommendation["window_id"] is not None
        else _null_span(f"статус {status!r} не выбирает конкретное окно")
    )
    rows = [
        _row("Статус", _esc(_RECOMMENDATION_STATUS_LABELS.get(status, status))),
        _row("Рекомендованное окно", window_html),
        _row("Обоснование", _esc(recommendation["explanation"])),
    ]
    return (
        "<section id=\"recommendation\"><h2>Рекомендация</h2><table>"
        + "".join(rows)
        + "</table></section>"
    )


def _limitations_section(result: dict[str, Any]) -> str:
    body = _list_or_none(result["limitations"], empty_reason="ограничений не зафиксировано")
    return f'<section id="limitations"><h2>Ограничения применимости</h2>{body}</section>'


def _warning_html(warning: dict[str, Any]) -> str:
    mechanism = warning["mechanism"]
    mechanism_label = (
        _esc(_MECHANISM_LABELS.get(mechanism, mechanism))
        if mechanism is not None
        else _null_span("предупреждение не привязано к одному механизму")
    )
    window_html = (
        f'<code>{_esc(warning["window_id"])}</code>'
        if warning["window_id"] is not None
        else _null_span("предупреждение относится ко всему результату, не к одному окну")
    )
    fetch_attempt_html = (
        f'<code>{_esc(warning["fetch_attempt_id"])}</code>'
        if warning["fetch_attempt_id"] is not None
        else _null_span("нет отдельно залогированной попытки обращения к источнику")
    )
    severity = warning["severity"]
    rows = [
        _row("Код", f"<code>{_esc(warning['code'])}</code>"),
        _row("Важность", _esc(_WARNING_SEVERITY_LABELS.get(severity, severity))),
        _row("Механизм", mechanism_label),
        _row("Сообщение", _esc(warning["message"])),
        _row("Окно", window_html),
        _row("Использованные записи (record_id)", _record_ids_html(warning["record_ids"])),
        _row("fetch_attempt_id", fetch_attempt_html),
    ]
    return "<table class=\"warning\">" + "".join(rows) + "</table>"


def _warnings_section(result: dict[str, Any]) -> str:
    warnings = result["warnings"]
    if not warnings:
        body = _null_span("предупреждений нет")
    else:
        body = "".join(_warning_html(w) for w in warnings)
    return f'<section id="warnings"><h2>Предупреждения</h2>{body}</section>'


def _data_manifest_section(result: dict[str, Any]) -> str:
    manifest = result["data_manifest"]
    if not manifest:
        body = _null_span("манифест пуст — ни одна запись не попала в расчёт")
    else:
        rows_html = "".join(
            "<tr>"
            f"<td><code>{_esc(entry['record_id'])}</code></td>"
            f"<td>{_esc(entry['source_id'])}</td>"
            f"<td>{_esc(entry['source_version'])}</td>"
            f"<td>{_esc(_RECORD_KIND_LABELS.get(entry['record_kind'], entry['record_kind']))}</td>"
            "</tr>"
            for entry in manifest
        )
        body = (
            "<table><thead><tr><th>record_id</th><th>source_id</th>"
            "<th>source_version</th><th>Вид записи</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
        )
    return (
        '<section id="data_manifest"><h2>Манифест использованных данных (data_manifest)</h2>'
        f"{body}</section>"
    )


_NO_ERROR_LOGGED_REASON = "нет зафиксированных ошибок этого источника"


def _source_status_row_html(status: dict[str, Any]) -> str:
    last_success_html = _optional_utc(
        status["last_success_at"],
        field="source_status.last_success_at",
        reason="источник ни разу не был успешно получен",
    )
    last_error_html = _optional_utc(
        status["last_error_at"], field="source_status.last_error_at", reason=_NO_ERROR_LOGGED_REASON
    )
    error_message_html = (
        _esc(status["last_error_message"])
        if status["last_error_message"] is not None
        else _null_span(_NO_ERROR_LOGGED_REASON)
    )
    return (
        "<tr>"
        f"<td>{_esc(status['source_id'])}</td>"
        f"<td>{last_success_html}</td>"
        f"<td>{last_error_html}</td>"
        f"<td>{error_message_html}</td>"
        f"<td>{'да' if status['frozen'] else 'нет'}</td>"
        f"<td>{'да' if status['quota_limited'] else 'нет'}</td>"
        "</tr>"
    )


def _source_status_section(result: dict[str, Any]) -> str:
    statuses = result["source_status"]
    if not statuses:
        body = _null_span("статусы источников не сохранены для этого результата")
    else:
        rows_html = "".join(_source_status_row_html(status) for status in statuses)
        body = (
            "<table><thead><tr><th>source_id</th><th>Последний успех</th>"
            "<th>Последняя ошибка</th><th>Сообщение об ошибке</th>"
            "<th>Заморожен</th><th>Квота исчерпана</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
        )
    return f'<section id="source_status"><h2>Статусы источников</h2>{body}</section>'


_STYLE = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { font-size: 1.4rem; }
h2 { font-size: 1.15rem; margin-top: 2rem; border-bottom: 1px solid #ccc; padding-bottom: .25rem; }
h3 { font-size: 1rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
th, td { border: 1px solid #ddd; padding: .35rem .5rem; text-align: left; vertical-align: top; }
th { background: #f4f4f4; white-space: nowrap; }
table.mechanism, table.warning { margin-bottom: .75rem; }
.null-value { color: #777; font-style: italic; }
.note { color: #555; font-size: .9rem; }
.exclusion { color: #8a5300; }
code { font-family: ui-monospace, monospace; }
section#meta { color: #444; }
"""


def export_result_html(result: dict[str, Any]) -> str:
    """Строит читаемый HTML-отчёт из уже сохранённого результата ``result``.

    Поднимает :class:`~src.export.errors.ExportError`, если ``result`` не
    проходит ``contracts/result.schema.json`` или несёт время без явного
    смещения — тот же принцип, что у ``export_result_json``: экспорт не
    строит правдоподобный отчёт из подозрительных данных (приёмка FN-36,
    п.3).
    """
    validate_result_or_raise(result)

    title = f"Результат расчёта {result['result_id']}"
    parts = [
        "<!doctype html>",
        '<html lang="ru">',
        "<head>",
        '<meta charset="utf-8" />',
        f"<title>{_esc(title)}</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        f"<h1>{_esc(title)}</h1>",
        '<section id="meta">',
        f"<p>Рассчитано: {_format_utc(result['computed_at'], field='computed_at')} · "
        f"алгоритм версии {_esc(result['algorithm_version'])}</p>",
        "</section>",
        _request_section(result),
        _orbit_section(result),
        _windows_section(result),
        _coverage_section(result),
        _recommendation_section(result),
        _limitations_section(result),
        _warnings_section(result),
        _data_manifest_section(result),
        _source_status_section(result),
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


__all__ = ["export_result_html"]
