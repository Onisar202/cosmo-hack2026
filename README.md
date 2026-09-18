# Сервис прогнозирования внешних рисков ВКД

Исследовательский прототип поддержки решений для планирования выхода в
открытый космос (ВКД) на МКС: сопоставляет космическую погоду и
метеороидную обстановку с окнами операции. Предметная постановка и
критерии оценки — `.ai/main-prompt.md` (§11, §12).

Этап FN-19 сдал каркас: слоистую структуру Python/FastAPI сервиса,
health-проверку и воспроизводимые проверки качества кода. FN-20 добавил
контракты (`contracts/`). FN-21 добавил неизменяемое хранилище оригиналов,
версий записей источников и результатов расчёта (`src/store/`). FN-22
добавляет коннектор космопогоды — NOAA SWPC, поток протонов >=10 МэВ
(`src/sources/swpc.py`). FN-24 добавляет коннектор орбитальных элементов
МКС с CelesTrak (`src/sources/orbit.py`) и первый расчётный модуль —
распространение орбиты по SGP4 (`src/domain/orbit/propagate.py`). Оба
коннектора регистрируют свои источники в общем реестре `sources.yaml`.
Расчётных эндпоинтов ещё нет — API, объединяющий эти модули с остальными
механизмами, появится вместе с задачей S1-07.

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
Success: no issues found in 21 source files

$ uv run pytest -v
tests/orbit/test_propagation.py::test_propagate_matches_independently_published_reference PASSED
tests/orbit/test_propagation.py::test_load_elements_epoch_matches_independently_parsed_epoch PASSED
tests/orbit/test_propagation.py::test_propagate_rejects_naive_datetime PASSED
tests/orbit/test_propagation.py::test_propagate_surfaces_sgp4_error_as_status_not_silence PASSED
tests/orbit/test_propagation.py::test_time_grid_is_configurable_and_respects_step PASSED
tests/orbit/test_propagation.py::test_time_grid_default_horizon_is_32_hours PASSED
tests/orbit/test_propagation.py::test_time_grid_max_hours_is_a_caller_parameter_not_a_constant PASSED
tests/orbit/test_propagation.py::test_time_grid_requires_aware_start_and_positive_step PASSED
tests/orbit/test_propagation.py::test_time_grid_always_covers_the_full_window_when_step_does_not_divide_it PASSED
tests/orbit/test_propagation.py::test_time_grid_covers_window_when_step_is_longer_than_the_window PASSED
tests/orbit/test_propagation.py::test_elements_age_hours_and_reconstruction_flag PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_accepts_real_recorded_response PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_rejects_corrupted_checksum PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_rejects_unexpected_norad_id PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_rejects_wrong_line_count PASSED
tests/orbit/test_propagation.py::test_parse_tle_response_rejects_mismatched_line1_line2_norad_id PASSED
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
tests/orbit/test_propagation.py::test_refined_elements_at_the_same_epoch_get_a_new_version_not_a_conflict PASSED
tests/sources/test_swpc.py::test_parse_response_filters_to_target_energy_channel PASSED
tests/sources/test_swpc.py::test_parse_response_keeps_plausible_flux_value PASSED
tests/sources/test_swpc.py::test_parse_response_maps_negative_sentinel_to_none_not_zero PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_boolean_flux PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_string_flux PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_non_finite_flux[Infinity] PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_non_finite_flux[-Infinity] PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_non_finite_flux[NaN] PASSED
tests/sources/test_swpc.py::test_parse_response_flags_yaw_flip_period_as_degraded PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_empty_array PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_empty_body PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_non_json_body PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_missing_target_channel PASSED
tests/sources/test_swpc.py::test_parse_response_rejects_non_list_payload PASSED
tests/sources/test_swpc.py::test_parse_response_accepts_time_tag_without_z_suffix_as_utc PASSED
tests/sources/test_swpc.py::test_to_record_input_has_no_published_at_and_is_not_replay_eligible PASSED
tests/sources/test_swpc.py::test_to_record_input_missing_value_has_no_unit PASSED
tests/sources/test_swpc.py::test_to_record_input_degraded_quality_for_yaw_flip PASSED
tests/sources/test_swpc.py::test_content_derived_source_version_allows_later_correction_as_new_row PASSED
tests/sources/test_swpc.py::test_repeated_fetch_of_same_value_is_idempotent PASSED
tests/sources/test_swpc.py::test_raw_bytes_are_canonical_per_entry_not_whole_rolling_response PASSED
tests/sources/test_swpc.py::test_http_fetch_retries_transient_5xx_then_succeeds PASSED
tests/sources/test_swpc.py::test_http_fetch_gives_up_after_max_retries PASSED
tests/sources/test_swpc.py::test_http_fetch_429_is_not_retried_and_raises_quota_error PASSED
tests/sources/test_swpc.py::test_http_fetch_429_retry_after_as_http_date_is_parsed_relative_to_now PASSED
tests/sources/test_swpc.py::test_http_fetch_timeout_after_retries_raises_timeout_error PASSED
tests/sources/test_swpc.py::test_http_fetch_does_not_retry_permanent_4xx PASSED
tests/sources/test_swpc.py::test_fetch_and_store_success_stores_records_and_updates_status PASSED
tests/sources/test_swpc.py::test_fetch_and_store_skips_network_within_ttl_then_refreshes_after PASSED
tests/sources/test_swpc.py::test_fetch_and_store_force_bypasses_ttl PASSED
tests/sources/test_swpc.py::test_fetch_and_store_frozen_never_touches_network_even_when_forced PASSED
tests/sources/test_swpc.py::test_fetch_and_store_disabled_source_never_touches_network PASSED
tests/sources/test_swpc.py::test_fetch_and_store_quota_429_gives_explicit_status_not_a_favorable_one PASSED
tests/sources/test_swpc.py::test_fetch_and_store_respects_quota_cooldown_until_retry_after_expires PASSED
tests/sources/test_swpc.py::test_fetch_and_store_timeout_gives_explicit_status PASSED
tests/sources/test_swpc.py::test_fetch_and_store_unexpected_format_gives_explicit_status_not_favorable PASSED
tests/sources/test_swpc.py::test_fetch_and_store_reports_error_when_every_sample_conflicts PASSED
tests/sources/test_swpc.py::test_fetch_and_store_reports_error_on_partial_conflict_not_stored PASSED
tests/sources/test_swpc.py::test_fetch_and_store_error_preserved_after_later_recovery PASSED
tests/sources/test_swpc.py::test_staleness_seconds_is_none_without_any_success PASSED
tests/sources/test_swpc.py::test_never_succeeded_source_counts_as_critically_stale PASSED
tests/sources/test_swpc.py::test_is_critically_stale_true_past_threshold_false_before PASSED
tests/sources/test_swpc.py::test_effective_status_disabled_config_forces_frozen_true PASSED
tests/sources/test_swpc.py::test_load_source_config_reads_real_sources_yaml PASSED
tests/sources/test_swpc.py::test_load_source_config_raises_for_unknown_source_id PASSED
tests/sources/test_swpc.py::test_live_smoke SKIPPED (live-smoke: реа...)
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
104 passed, 1 skipped
```

`test_live_smoke` пропускается намеренно: детерминированные тесты парсера
гоняются на сохранённых реальных ответах (`tests/fixtures/sources/swpc/`,
см. её README про их происхождение), а не на живой сети
(.ai/main-prompt.md §9 «тесты детерминированы, live-smoke отдельно»).

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
  соответствующего коннектора (.ai/main-prompt.md §7). FN-22 заводит секцию
  реестра `space_weather` (NOAA SWPC, общественное достояние без
  ограничений на локальное хранение), FN-24 — записи `celestrak-gp` и
  `space-track-gp-history` в секции `sources`; хранилище уже сейчас
  соблюдает эту политику тем, что ничего не перезаписывает и не удаляет
  через публичный интерфейс.

## Источники (`src/sources/`)

Слой получения: коннекторы, парсеры, нормализация ответа источника в
`RecordInput` для `src/store/` (.ai/main-prompt.md §8 — не интерпретирует
данные и не считает физику). Первый и пока единственный коннектор —
NOAA SWPC, интегральный поток протонов `>=10 МэВ` (Механизм 1
«Радиационная обстановка», main-prompt.md §11).

- **`src/sources/http.py`** — общий HTTP-клиент для всех коннекторов:
  раздельные таймауты на соединение и на чтение, ограниченное число
  повторов с экспоненциальной задержкой для временных ошибок (сеть/таймаут/
  5xx), `429` обрабатывается отдельно и без немедленного повтора
  (.ai/backend-prompt.md §3).
- **`src/sources/swpc.py`** — коннектор и парсер продукта
  `integral-protons-1-day` (первичный спутник GOES, канал `>=10 MeV`).
  Продукт не несёт времени публикации — только время измерения
  (`time_tag`) — поэтому `published_at` каждой записи всегда `null`, а
  `replay_eligible` всегда `false`: это ожидаемо и явно допущено приёмкой
  задачи («допускается отсутствие доказанной публикации текущего
  измерения»). Строгий replay из прошлого по космопогоде реализуется
  другой линией (архивные сводки NOAA SWPC/NCEI с явным Issued) — вне
  объёма этой задачи.
  - Пустой ответ, невалидный JSON, отсутствие ожидаемого канала энергии —
    явная ошибка формата (`SwpcFormatError`), не пустой список (иначе
    смена формата источника читалась бы как «нет активности»).
  - Отрицательный/невалидный отсчёт потока (сентинел продукта) сохраняется
    как `value=None`, а не отбрасывается и не заменяется нулём.
  - `source_version` производится из содержания измерения (у продукта нет
    собственной версии): повторная выборка того же значения — идемпотентна,
    а переиздание того же `time_tag` с другим значением становится новой
    записью рядом со старой, а не конфликтом дедупликации.
  - `fetch_and_store(...)` — единый управляемый шлюз: TTL по конфигу
    (`sources.yaml`) реализует периодический refresh без реального
    сетевого вызова, пока последний успех не устарел; `force=True`
    обходит TTL, но не обходит заморозку/отключение.
- **`src/sources/status.py`** — статус источника как отдельная сущность
  (`SourceStatus`: `last_success_at`, `last_error_at`,
  `last_error_message`, `frozen`, `quota_limited` — форма совпадает с
  `contracts/result.schema.json` → `source_status[*]`), плюс
  `staleness_seconds`/`is_critically_stale` для решения «оценить
  невозможно» на уровне домена (последующая задача).
- **Отключение и заморозка источника** (main-prompt.md §5, §10;
  .ai/backend-prompt.md §3) — два независимых, документированных здесь
  переключателя, оба действуют без перезапуска сервиса:
  - **заморозка** — оперативный переключатель в памяти
    (`SourceStatusRegistry.freeze`/`.unfreeze`); не трогает уже сохранённые
    записи, только останавливает новые обращения к источнику;
  - **отключение** — статическая настройка `enabled: false` в записи
    источника в `sources.yaml`, перечитывается заново при каждом обращении
    (файл не кешируется), поэтому правка файла тоже действует без
    перезапуска.
  - Оба состояния снаружи видны через одно и то же поле контракта
    `frozen` (`effective_status`) — контракт не различает причину.
- **`sources.yaml`** — реестр: адрес, продукт, единицы, охват, лицензия,
  таймауты, TTL периодического refresh и порог критического устаревания
  для каждого источника (main-prompt.md §7 — конфигурация, а не код).

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
src/
├── api/        # FastAPI: тонкие роутеры, валидация — сейчас только /health
├── config.py   # Настройки из env, без секретов
├── sources/    # Получение и нормализация — http.py/swpc.py/status.py (NOAA SWPC),
│               # orbit.py (CelesTrak GP/TLE)
├── store/      # Хранение, версии, выборка по as_of — schema.py/records.py/results.py
├── domain/     # Расчёты: spaceweather, orbit (propagate.py — SGP4), mmod, lighting, windows
└── export/     # HTML и JSON из сохранённого результата (пока не реализовано)
sources.yaml    # Реестр источников (main-prompt.md §7): space_weather (noaa-swpc-proton-flux),
                # sources (celestrak-gp, space-track-gp-history, MMOD)
tests/
├── sources/    # test_swpc.py + fixtures/sources/swpc/ (сохранённые реальные ответы)
├── orbit/      # test_propagation.py
├── fixtures/orbit/  # TLE-фикстуры и независимый эталон SGP4 verification
├── store/      # test_versions.py, test_as_of.py
└── test_health.py
```

Слой `web/` (React + TypeScript + Vite) — зона ответственности 4, в эту
задачу не входит.
