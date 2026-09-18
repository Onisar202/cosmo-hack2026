# Сервис прогнозирования внешних рисков ВКД

Исследовательский прототип поддержки решений для планирования выхода в
открытый космос (ВКД) на МКС: сопоставляет космическую погоду и
метеороидную обстановку с окнами операции. Предметная постановка и
критерии оценки — `.ai/main-prompt.md` (§11, §12).

Этап FN-19 сдал каркас: слоистую структуру Python/FastAPI сервиса,
health-проверку и воспроизводимые проверки качества кода. FN-20 добавил
контракты (`contracts/`). FN-21 добавил неизменяемое хранилище оригиналов,
версий записей источников и результатов расчёта (`src/store/`). FN-24
добавляет первый коннектор источника и первый расчётный модуль: получение
текущих орбитальных элементов МКС с CelesTrak (`src/sources/orbit.py`) и
распространение орбиты по SGP4 (`src/domain/orbit/propagate.py`), а также
реестр источников `sources.yaml`. Расчётных эндпоинтов ещё нет — API,
объединяющий эти модули с остальными механизмами, появится вместе с
задачей S1-07.

## Стек

Python 3.11+, FastAPI, Pydantic v2, [SGP4](https://pypi.org/project/sgp4/),
httpx, [uv](https://docs.astral.sh/uv/) как менеджер пакетов и виртуальных
окружений.

## Установка

```bash
uv sync --locked --all-groups
```

Команда создаёт `.venv` и ставит зависимости строго по `uv.lock`.

## Настройка

```bash
cp .env.example .env
```

Обязательные и необязательные переменные окружения описаны с
комментариями в `.env.example`. Значений ключей и других секретов там
нет — на этом этапе сервис не обращается к внешним источникам.
Обязательные настройки (например, `APP_ENV`) проверяются при создании
`Settings` (`src/config.py`), то есть при старте приложения: некорректный
или неполный `.env` не даёт сервису запуститься.

## Запуск

Одна команда локального запуска:

```bash
uv run python -m src.api.app
```

Сервис поднимается на `http://0.0.0.0:8000`. Проверка:

```bash
curl http://127.0.0.1:8000/health
```

```json
{"status":"ok","service":"vkd-risk-service","app_env":"development","time":"2026-09-18T21:30:58.540824Z"}
```

## Проверки

Те же проверки использует CI (`.github/workflows/ci.yml`).

```bash
uv run ruff check .    # линтер
uv run mypy src         # проверка типов
uv run pytest -v         # тесты (без сети)
```

Результаты на чистом checkout (Python 3.11.15, зависимости из `uv.lock`):

```text
$ uv run ruff check .
All checks passed!

$ uv run mypy src
Success: no issues found in 18 source files

$ uv run pytest -v
tests/orbit/test_propagation.py::test_propagate_matches_independently_published_reference PASSED
tests/orbit/test_propagation.py::test_load_elements_epoch_matches_independently_parsed_epoch PASSED
tests/orbit/test_propagation.py::test_propagate_rejects_naive_datetime PASSED
tests/orbit/test_propagation.py::test_propagate_surfaces_sgp4_error_as_status_not_silence PASSED
tests/orbit/test_propagation.py::test_time_grid_is_configurable_and_respects_step PASSED
tests/orbit/test_propagation.py::test_time_grid_default_horizon_is_32_hours PASSED
tests/orbit/test_propagation.py::test_time_grid_max_hours_is_a_caller_parameter_not_a_constant PASSED
tests/orbit/test_propagation.py::test_time_grid_requires_aware_start_and_positive_step PASSED
tests/orbit/test_propagation.py::test_elements_age_hours_and_reconstruction_flag PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_accepts_real_recorded_response PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_rejects_corrupted_checksum PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_rejects_unexpected_norad_id PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_rejects_wrong_line_count PASSED
tests/orbit/test_propagation.py::test_fetch_current_tle_returns_body_on_200 PASSED
tests/orbit/test_propagation.py::test_fetch_current_tle_raises_quota_error_on_429 PASSED
tests/orbit/test_propagation.py::test_fetch_current_tle_raises_source_error_on_non_200 PASSED
tests/orbit/test_propagation.py::test_fetch_current_tle_raises_source_error_on_empty_200_body PASSED
tests/orbit/test_propagation.py::test_fetch_current_tle_raises_source_error_on_timeout PASSED
tests/orbit/test_propagation.py::test_require_supported_mode_allows_current PASSED
tests/orbit/test_propagation.py::test_require_supported_mode_rejects_historical_modes[historical_analysis] PASSED
tests/orbit/test_propagation.py::test_require_supported_mode_rejects_historical_modes[historical_forecast] PASSED
tests/orbit/test_propagation.py::test_fetch_elements_for_request_rejects_historical_without_network_call[historical_analysis] PASSED
tests/orbit/test_propagation.py::test_fetch_elements_for_request_rejects_historical_without_network_call[historical_forecast] PASSED
tests/orbit/test_propagation.py::test_fetch_elements_for_request_current_mode_returns_parsed_elements PASSED
tests/orbit/test_propagation.py::test_build_orbital_elements_record_is_never_replay_eligible PASSED
tests/store/test_as_of.py::test_publication_after_cutoff_is_excluded PASSED
tests/store/test_as_of.py::test_records_without_published_at_never_selected PASSED
tests/store/test_as_of.py::test_newer_publication_after_cutoff_does_not_leak_even_as_a_refinement PASSED
tests/store/test_as_of.py::test_select_as_of_returns_latest_eligible_version_of_the_same_product PASSED
tests/store/test_as_of.py::test_select_as_of_filters_by_source_id_and_record_kind PASSED
tests/store/test_as_of.py::test_select_as_of_at_exact_cutoff_is_inclusive PASSED
tests/store/test_versions.py::test_later_refinement_does_not_overwrite_old PASSED
tests/store/test_versions.py::test_duplicate_insert_is_idempotent_and_no_double_impact PASSED
tests/store/test_versions.py::test_missing_published_at_is_not_replay_eligible PASSED
tests/store/test_versions.py::test_null_value_is_preserved_not_replaced_with_zero PASSED
tests/store/test_versions.py::test_original_is_recoverable_by_record_id_with_matching_checksum PASSED
tests/store/test_versions.py::test_checksum_mismatch_is_detected PASSED
tests/store/test_versions.py::test_store_module_exposes_no_update_or_delete PASSED
tests/store/test_versions.py::test_result_is_immutable_recompute_creates_new_result_id PASSED
tests/store/test_versions.py::test_store_result_rejects_manifest_referencing_unknown_record PASSED
tests/store/test_versions.py::test_different_results_parameters_are_isolated PASSED
tests/store/test_versions.py::test_records_and_results_survive_restart PASSED
tests/store/test_versions.py::test_record_input_rejects_naive_datetime PASSED
tests/store/test_versions.py::test_record_input_rejects_empty_source_version PASSED
tests/store/test_versions.py::test_duplicate_key_with_different_content_is_a_conflict_not_a_duplicate PASSED
tests/store/test_versions.py::test_duplicate_key_with_identical_content_is_idempotent PASSED
tests/store/test_versions.py::test_duplicate_key_with_same_original_but_different_normalized_field_is_a_conflict[override0] PASSED
tests/store/test_versions.py::test_duplicate_key_with_same_original_but_different_normalized_field_is_a_conflict[override1] PASSED
tests/store/test_versions.py::test_duplicate_key_with_same_original_but_different_normalized_field_is_a_conflict[override2] PASSED
tests/store/test_versions.py::test_duplicate_key_with_same_original_but_different_normalized_field_is_a_conflict[override3] PASSED
tests/store/test_versions.py::test_published_at_offset_is_normalized_to_utc_before_comparison PASSED
tests/test_health.py::test_health_returns_200_ok PASSED
tests/test_health.py::test_health_time_is_utc_aware PASSED
tests/test_health.py::test_settings_requires_app_env PASSED
tests/test_health.py::test_settings_rejects_unknown_app_env PASSED
55 passed
```

## Хранилище (`src/store/`)

Неизменяемое хранилище оригиналов ответов источников, версий нормализованных
записей и результатов расчёта (форма — `contracts/record.schema.json` и
`contracts/result.schema.json`). Ядро — приложи-только: публичный интерфейс
(`src/store/records.py`, `src/store/results.py`) не предоставляет ни одной
операции обновления или удаления сохранённой записи или результата
(.ai/backend-prompt.md §1–2).

- **Нормализованные записи** — таблица SQLite `source_records`. Дедупликация
  по `(source_id, provider_record_id, source_version)`: повторная вставка
  того же сочетания создаёт нет-оп только когда совпадают и оригинал (по
  контрольной сумме), и все значимые нормализованные поля (`value`, `unit`,
  `published_at`, `quality`, `spatial_context` и т.п.; `fetched_at` не
  входит — момент получения того же оригинала законно отличается между
  повторными обращениями). Любое расхождение — конфликт версии у
  поставщика или ошибка нормализации выше по пайплайну
  (`DuplicateKeyConflictError`), а не тихая замена. Позднее уточнение того
  же продукта — новая строка с новым `source_version`, прежняя не трогается.
- **Оригиналы ответов** — контент-адресуемое хранилище на диске
  (`RawOriginalStore`), путь файла определяется его SHA-256; оригинал и
  контрольная сумма восстанавливаются по `record_id` (`get_original`),
  несовпадение контрольной суммы — ошибка, а не тихая порча данных.
- **`replay_eligible`** вычисляется при записи из `published_at` и
  `source_version`: неизвестное время публикации (`published_at is None`)
  или пустая версия — запись навсегда непригодна для строгого прогноза из
  прошлого (пустая версия отклоняется на вставке целиком, а не тихо
  помечается непригодной), задним числом флаг не восстанавливается.
- **`select_as_of(as_of, ...)`** — основной запрос строгого replay: только
  записи с `published_at <= as_of` и `replay_eligible = true`, внутри одного
  продукта — с наибольшим пригодным `published_at` (самое позднее известное
  к этому моменту уточнение). Публикация позже `as_of` в выборку не попадает,
  даже если это более новая версия того же продукта. Все времена перед
  сравнением нормализуются к UTC и сериализуются в фиксированный по ширине
  формат — иначе запись со смещением (например `00:30-05:00`, то есть
  `05:30Z`) могла бы пройти строковое сравнение с отсечением `03:00Z`
  лексикографически, хотя фактически опубликована позже.
- **Результаты расчёта** — таблица `calculation_results`, тоже без
  обновления/удаления: пересчёт создаёт новый `result_id`. `store_result`
  всегда (без возможности отключить) проверяет, что каждая запись
  `data_manifest` реально существует в хранилище и совпадает с ней по
  `source_id`/`source_version`/`record_kind` — манифест не может быть
  правдоподобным, но не проверенным списком.
- **Хранение и перезапуск.** SQLite-файл и каталог оригиналов — пути из
  конфига (`STORE_DB_PATH`, `STORE_RAW_DIR`), по умолчанию подкаталог
  проекта, а не временная директория контейнера: перезапуск сервиса не
  теряет сохранённые записи и результаты (проверено тестом).
- **Срок хранения и лицензии.** У записей и результатов нет TTL и фоновой
  очистки — хранилище само по себе ничего не вытесняет по времени
  (.ai/main-prompt.md §2–3). Условия лицензии конкретного источника
  (допустимость локального хранения оригинала, требования к атрибуции,
  ограничения на срок хранения) фиксируются в `sources.yaml` при добавлении
  соответствующего коннектора (.ai/main-prompt.md §7); хранилище само
  соблюдает эту политику тем, что ничего не перезаписывает и не удаляет
  через публичный интерфейс.

## Орбита (`src/sources/orbit.py`, `src/domain/orbit/`)

Первый коннектор источника и первый расчётный модуль сервиса.

- **Получение (`src/sources/orbit.py`).** Текущие орбитальные элементы
  (GP/TLE) МКС, NORAD `25544`, с CelesTrak
  (`fetch_current_tle`/`parse_tle_response`) — жёсткий таймаут на
  соединение и чтение, код `429` обрабатывается отдельно
  (`OrbitSourceQuotaError`), пустое тело при коде `200` и повреждённая
  контрольная сумма TLE — явные статусы (`OrbitSourceError`,
  `CorruptedElementsError`), а не тихо принятые данные
  (.ai/main-prompt.md §11 «Отказ/старые/повреждённые элементы дают
  статус»). `build_orbital_elements_record` нормализует ответ в
  `RecordInput` (`record_kind=orbital_elements`) для `src/store/`;
  `published_at` всегда `None` (CelesTrak не публикует время выпуска
  набора — см. `sources.yaml`), поэтому хранилище всегда вычисляет
  `replay_eligible=false` для этих записей — текущие элементы не могут
  быть тихо использованы для строгого `historical_forecast`.
- **Исторический режим.** `fetch_elements_for_request` поддерживает только
  `mode="current"`: для `historical_analysis`/`historical_forecast`
  поднимается `HistoricalElementsUnsupportedError` до какого-либо сетевого
  обращения — коннектор Space-Track `GP_HISTORY` появится отдельной
  задачей (см. `sources.yaml`, продукт `space-track-gp-history`); до тех
  пор современные элементы не подставляются вместо исторических
  (.ai/main-prompt.md §11 «Траектория»).
- **Расчёт (`src/domain/orbit/propagate.py`).** Чистые функции без сети и
  без хранилища: `load_elements`/`propagate` оборачивают SGP4, принимают
  элементы и явную сетку моментов времени аргументами (не читают "сейчас").
  `time_grid` строит конфигурируемую сетку до `max_hours` (по умолчанию 32
  часа — верхняя граница расчётного интервала из постановки), шаг и
  горизонт — параметры вызывающей стороны, не константы модуля.
  `elements_age_hours`/`is_reconstructed_geometry` дают давность элементов
  и признак реконструкции геометрии для непроверенного `as_of`. Система
  координат — TEME, единицы — км и км/с, без скрытого преобразования.
- **Точность.** `tests/orbit/test_propagation.py` сверяет `propagate()` с
  официальным независимым эталоном SGP4 verification (Vallado, Crawford,
  Hujsak & Kelso, "Revisiting Spacetrack Report #3", 2006; см.
  `tests/fixtures/orbit/README.md`) — не с расчётом, повторяющим
  собственную реализацию (.ai/main-prompt.md §9, п.5).
- **Реестр источников `sources.yaml`.** Продукт `celestrak-gp` (этот
  коннектор) и `space-track-gp-history` (ещё не реализован) — с URL,
  единицами, охватом, частотой обновления, наличием `published_at` и
  пригодностью для строгого replay (.ai/main-prompt.md §7, §10).

## Структура проекта

Полное описание слоёв и границ между ними — `.ai/main-prompt.md`, §8.

```text
sources.yaml    # Реестр источников: celestrak-gp, space-track-gp-history
src/
├── api/        # FastAPI: тонкие роутеры, валидация — сейчас только /health
├── config.py   # Настройки из env, без секретов
├── sources/    # Получение и нормализация — orbit.py (CelesTrak GP/TLE)
├── store/      # Хранение, версии, выборка по as_of — schema.py/records.py/results.py
├── domain/     # Расчёты: spaceweather, orbit (propagate.py — SGP4), mmod, lighting, windows
└── export/     # HTML и JSON из сохранённого результата (пока не реализовано)
tests/
├── orbit/      # test_propagation.py
├── fixtures/orbit/  # TLE-фикстуры и независимый эталон SGP4 verification
├── store/      # test_versions.py, test_as_of.py
└── test_health.py
```

Слой `web/` (React + TypeScript + Vite) — зона ответственности 4, в эту
задачу не входит.
