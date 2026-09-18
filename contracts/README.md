# Контракты (запрос, запись, результат)

Этот каталог — единственный источник истины о форме данных, которыми
обмениваются слои сервиса прогнозирования рисков ВКД, и о форме API S1-07.
Схемы читаются вместе с [`.ai/main-prompt.md`](../.ai/main-prompt.md) §1–4,
§11–12 и [`.ai/backend-prompt.md`](../.ai/backend-prompt.md) §1, §6 — эти
документы источник смысла, здесь — источник формы.

**Изменение любой из трёх схем обновляет все четыре фикстуры в этом же PR**
(.ai/main-prompt.md §10, п.3). Участники зоны 3 (аналитика/API) и зоны 4
(интерфейс) уведомляются об изменении.

## Файлы

| Файл | Что описывает |
| --- | --- |
| `request.schema.json` | Тело запроса на расчёт (используется и как форма запроса эндпоинта, и как вложенный объект `result.request`). |
| `record.schema.json` | Неизменяемая запись источника (наблюдение / прогноз / предупреждение / орбитальные элементы), как она лежит в `store/`. |
| `result.schema.json` | Неизменяемый сохранённый результат расчёта — то, что отдают API, интерфейс и обе выгрузки (HTML/JSON). |
| `fixtures/success.json` | Полный успешный расчёт (`historical_forecast`): два окна, окно A выигрывает по правилу предпочтения (меньший максимальный уровень механизма 1). |
| `fixtures/incomplete.json` | Механизм MMOD ещё `not_implemented`, по механизму 1 в одном из окон `missing_data` — оба окна исключены из сравнения (`all_windows_excluded`), демонстрирует различение `not_implemented`/`missing_data` и частичную (не нулевую и не выдуманную) `coverage_fraction`. |
| `fixtures/source-error.json` | Архив источника космической погоды отвечает 429 (`source_error`, `quota_limited: true`) независимо от отдельного объективного пробела в архиве (`coverage.archive_gaps`); отказ источника не превращается в «фон». |
| `fixtures/equal-windows.json` | Два окна идентичны по всем шагам правила предпочтения — `recommendation.status = "tie"`, рекомендация не выдаётся. |

Все четыре фикстуры — **демонстрационные** (см. поле `limitations` в каждой)
и не являются результатом научного расчёта; они существуют, чтобы зафиксировать
форму контракта и валидны по `result.schema.json`.

## Ключевые решения контракта

### Три режима и `as_of`

- `mode` ∈ `{current, historical_analysis, historical_forecast}`.
- `as_of` **обязателен** только при `mode = historical_forecast` (момент
  отсечения: во вход попадают записи с `published_at <= as_of` и
  `replay_eligible == true`).
- `as_of` **запрещён** при `mode = current` (как явно указано в постановке)
  и при `mode = historical_analysis`. Решение по `historical_analysis`:
  постановка не описывает это явно, но `historical_analysis` — разбор по
  полному архиву без отсечения (в отличие от `historical_forecast`); допускать
  необязательный `as_of` для него было бы двусмысленно (игнорируется? частично
  применяется?), поэтому схема запрещает поле целиком. Если контуру 3 нужен
  другой контракт для `historical_analysis` — это меняет `request.schema.json`
  и все четыре фикстуры одним PR.
- В `request` (в том числе вложенном в `result.request`) поле `as_of`
  либо обязано присутствовать (`historical_forecast`), либо обязано
  отсутствовать целиком (`current`, `historical_analysis`) — не `null`.
  В `result.as_of` (верхнеуровневый дубликат для удобного доступа) поле
  **всегда присутствует**: `null` для `current`/`historical_analysis`,
  ISO-строка для `historical_forecast`.
- **`as_of <= start_at` — обязательное правило сервисного валидатора**, не
  выражается в JSON Schema (нужно сравнение двух дат). Отсечение позже
  начала окна превращает строгий прогноз из прошлого в ретроспективный
  разбор по факту: запись, опубликованная уже после начала окна, но до
  `as_of`, пройдёт фильтр `published_at <= as_of`, хотя описывает то, что
  случилось внутри самого окна, а не то, что было предсказуемо заранее.
  Из-за этого была переделана `fixtures/success.json` (round 1 ревью):
  раньше `as_of` стоял после обоих окон и в `data_manifest` попадала
  запись-наблюдение, опубликованная внутри окна A; теперь `as_of`
  предшествует обоим окнам, а `mechanismAssessment.record_ids` для
  `space_weather` ссылаются только на `record_kind = forecast/warning`,
  опубликованные до отсечения.
- **`result.mode`/`result.as_of` обязаны совпадать с `result.request.mode`/
  `result.request.as_of`.** Совпадение `mode` схема проверяет полностью
  (три ветки `allOf` по значению `mode` требуют то же значение внутри
  `request.mode` — объект с `mode="current"` наверху и
  `request.mode="historical_forecast"` невалиден). Точное строковое
  равенство `as_of` JSON Schema не выражает (сравнение двух значений в
  разных точках документа вне её возможностей без нестандартных
  расширений) — эту точную проверку выполняет сервисный валидатор перед
  сохранением результата.

### Часы и период поиска

- `duration_hours`: 1–8.
- `search_window_hours`: 0–24 — насколько далеко после `start_at` ищутся
  альтернативные начала окна. `search_end_at = start_at + search_window_hours`.
- Расчётный интервал сервер считает сам:
  `calc_end_at = search_end_at + duration_hours`, что при максимумах
  (24 + 8) даёт ровно 32 часа от `start_at` — верхнюю границу из постановки.
  Эта арифметика **не выражается в JSON Schema** (JSON Schema не умеет
  складывать даты) и реализуется в Pydantic-валидаторе на стороне сервиса
  зоны 3; здесь она задокументирована как обязательная для реализации.

### Обязательный исторический период

- Постановка требует однозначной поддержки периода **1 мая — 30 июня 2024**
  с включёнными границами (`start_at` ровно в 00:00:00Z 1 мая — валиден,
  ровно в 23:59:59Z 30 июня — валиден) для `historical_analysis` и
  `historical_forecast`. Дата вне периода — понятная ошибка с указанием
  поддерживаемого периода, а не пустой результат
  (`.ai/backend-prompt.md` §6).
- Эта граница **не выражена в JSON Schema** по той же причине (календарная
  арифметика с учётом произвольного смещения часового пояса не
  формализуется в JSON Schema без нечитаемых регулярных выражений) —
  реализуется на уровне сервиса. Единственный источник истины для констант
  границ периода — этот README: `ARCHIVE_START = 2024-05-01T00:00:00Z`,
  `ARCHIVE_END = 2024-06-30T23:59:59.999999Z` (включительно).
- Непокрытие внутри архива на более мелком масштабе (конкретный источник не
  имеет данных за конкретный час/день внутри разрешённого периода) — это уже
  не ошибка валидации запроса, а часть результата: `result.coverage.archive_gaps`
  (см. `fixtures/source-error.json`). Так «архивный пробел за пределами
  разрешённого периода запроса» (запрос отклонён) отличается от «пробела
  внутри разрешённого периода» (запрос выполнен, пробел виден в результате).

### `null`, а не `0`, и три состояния данных

- `record.value`/`record.unit` типизированы по `record_kind`: для
  `orbital_elements` — структурированный объект (`$defs/orbitalElementsValue`,
  обязательное поле `raw`) и `unit` всегда `null`; для остальных видов —
  `value: number | null`, и когда `value` — число, `unit` **обязан** быть
  непустой строкой (а не `null`) — нельзя одновременно иметь измерение и не
  указать единицу. `null` означает «нет данных», а не «ноль» — легитимный
  измеренный `0` остаётся `0` с непустой `unit`.
- `record.record_id` — суррогатный ключ хранилища, не идентификатор
  поставщика. Отдельное обязательное поле `record.provider_record_id` несёт
  устойчивый идентификатор продукта/сообщения у поставщика (например номер
  уведомления DONKI) — дедупликация повторных сообщений ведётся по паре
  `(source_id, provider_record_id)`, а не по `record_id` хранилища и не по
  времени получения (`.ai/main-prompt.md` §2). Хранилище обязано обеспечить
  уникальность тройки `(source_id, provider_record_id, source_version)` —
  это уже не выражается в JSON Schema (ограничение на уровне БД/хранилища).
- `record.published_at`: `null` — время публикации неизвестно; при этом
  `replay_eligible` **обязан** быть `false` (схема требует это через
  `if`/`then`) — восстановить право на строгий replay задним числом по дате
  измерения нельзя (`.ai/main-prompt.md` §1).
- `result` per-mechanism (`mechanismAssessment.status`) различает **шесть**
  состояний, а не «есть/нет»:
  `ok | not_implemented | missing_data | stale_data | source_error | beyond_horizon`.
  `not_implemented` — механизм ещё не реализован в этой версии сервиса и не
  имитируется готовым; это отдельное значение от `missing_data` (источник
  недоступен) и от `beyond_horizon` (окно вне горизонта прогноза — «не
  покрыто», а не «спокойно»). При любом статусе, кроме `ok`, `max_level` и
  `exceedance_hours_by_level` обязаны быть `null` — схема запрещает
  подставлять придуманное значение (`if status != "ok" then max_level == null`).
- `mechanismAssessment.exceedance_hours_by_level` — **объект с длительностью
  превышения по каждому применимому порогу**, а не одно число для
  максимального уровня (main-prompt §11: «суммарная длительность превышения
  каждого порога»). Для `space_weather` — обязательные ключи `{S1, S2, S3}`
  (шкала NOAA), для `mmod` — `{elevated, pronounced}`; форма зависит от
  `mechanism` и проверяется схемой. Монотонность (время на S3 ⊆ время на
  S2 ⊆ время на S1) JSON Schema не проверяет — это гарантирует сам расчёт.

### Равенство окон и отсутствие рекомендации

`result.recommendation.status` ∈
`selected | tie | insufficient_basis | all_windows_excluded`.
`window_id` обязан быть непустой строкой только при `selected`, иначе —
`null` (схема требует это условно). Так контракт прямо выражает исход
«окна равнозначны» (`tie`, см. `fixtures/equal-windows.json`) и исход
«оснований для рекомендации нет» (`insufficient_basis` /
`all_windows_excluded`, см. `fixtures/incomplete.json` и
`fixtures/source-error.json`), а не только успешный выбор.

### Прослеживаемость

Каждая `mechanismAssessment` несёт `record_ids` — записи, на которых
основана оценка; каждый `warning` — тоже. `result.data_manifest` перечисляет
фактически использованные записи с версиями. `result.orbit` несёт источник,
эпоху элементов, их давность (`elements_age_hours`) и флаг реконструкции
(`is_reconstructed`) — современные элементы никогда не подставляются вместо
исторических; при `is_reconstructed = true` это явно видно в результате.

**Каждое критическое предупреждение доказуемо.** `warning.record_ids` может
быть пустым (не для каждого вывода существует сохранённая запись — например
отказ источника означает, что запись вообще не была создана), но тогда
`warning.fetch_attempt_id` обязан ссылаться на устойчивый идентификатор
залогированной (в том числе неуспешной) попытки обращения к источнику —
такая попытка и её результат/тело ответа сохраняются наравне с успешными
(`.ai/backend-prompt.md` §5 «Логируется каждое обращение к источнику»).
Схема требует хотя бы одно из двух (`record_ids` непуст **или**
`fetch_attempt_id` непуст) для `severity = critical` — от критического
вывода нельзя дойти до пустоты. Для `info`/`advisory` это не обязательно:
например предупреждение о нереализованном механизме (`mmod-not-implemented`)
не связано ни с записью, ни с попыткой получения — попытки не было в
принципе, и требовать evidence для него значило бы просить выдумать его.

## Endpoint shapes для S1-07 (контур 3, аналитика/API)

Контракт задаёт форму, но не сам HTTP-протокол; ниже — минимальная фиксация
для контура 3, чтобы не расходиться при реализации API:

- `POST /api/calculations` — тело запроса по `request.schema.json`; отвечает
  `202 Accepted` с `{ "task_id": string, "status": "pending" }` (длительная
  операция — реестр задач в процессе, не брокер очередей, см.
  `.ai/backend-prompt.md` §6).
- `GET /api/calculations/{task_id}` — статус задачи:
  `{ "task_id": string, "status": "pending" | "running" | "done" | "failed",
  "result_id": string | null, "error": { "code": string, "message": string } | null }`.
  Трассировок в ответе нет (см. `.ai/backend-prompt.md` §6).
- `GET /api/results/{result_id}` — объект по `result.schema.json`. Тот же
  объект используют HTML- и JSON-выгрузка (`export/`) и интерфейс — без
  отдельной сборки из другого источника.
- `GET /api/results` — список сохранённых результатов (постранично), поля —
  подмножество `result.schema.json` (`result_id`, `computed_at`, `mode`,
  `as_of`, `recommendation.status`) без полных `windows`/`data_manifest`.
- `POST /api/sources/refresh` — принудительное обновление источников
  (только для `mode = current`); отвечает списком по форме
  `result.source_status[*]`.
- `GET /api/sources/status` — статусы всех источников, форма —
  `result.source_status` в виде массива без привязки к конкретному расчёту.
- Формат ошибок везде единый: `{ "error": { "code": string, "message": string } }`,
  без трассировок (`.ai/backend-prompt.md` §6).

Конкретные пути, коды статусов и пагинация — предмет реализации S1-07;
любое изменение формы `request`/`record`/`result` при этом по-прежнему
проходит через это контур: правится схема и все четыре фикстуры.

## Проверка

Схемы — JSON Schema Draft 2020-12; `result.request` ссылается на
`request.schema.json` через `$ref` относительным путём (резолвится
относительно `$id`/пути каталога — см. `referencing`/`ajv` с базовым URI
каталога `contracts/`).

```bash
pip install jsonschema referencing
python3 - <<'PY'
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

BASE = Path("contracts")
schemas = {n: json.loads((BASE / f"{n}.schema.json").read_text())
           for n in ("request", "record", "result")}
registry = Registry().with_resources(
    (s["$id"], Resource.from_contents(s)) for s in schemas.values()
)
for name, schema in schemas.items():
    Draft202012Validator.check_schema(schema)

validator = Draft202012Validator(schemas["result"], registry=registry)
for fixture in sorted((BASE / "fixtures").glob("*.json")):
    data = json.loads(fixture.read_text())
    errors = list(validator.iter_errors(data))
    print(fixture.name, "OK" if not errors else errors)
PY
```

Ожидаемый результат: все четыре фикстуры — `OK`, схемы проходят
`check_schema` без ошибок.
