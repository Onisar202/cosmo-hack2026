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
распространение орбиты по SGP4 (`src/domain/orbit/propagate.py`). FN-23
подтверждает пригодность архивов космопогоды для строгого прогноза из
прошлого — NASA CCMC DONKI (основная линия, весь период
01.05–30.06.2024) и NOAA SWPC Forecast Discussion из архива NCEI
(дополнительная линия, с честно зафиксированным пробелом 15.05–16.06.2024)
— и нормализует их в записи хранилища (`src/sources/archive_probe.py`,
доказательства — `docs/method.md`). Все коннекторы и зонд регистрируют
свои источники в общем реестре `sources.yaml`. FN-26 (S1-07) добавляет
расчётный API (`src/api/`): запуск задания, статус, сохранённый результат,
список результатов, статусы и принудительное обновление источников — с
реестром задач в процессе и изоляцией конкурентных запросов.
Интерпретация обоих обязательных механизмов воздействия (космическая
погода, MMOD) и строгий исторический режим орбиты (Space-Track
GP_HISTORY) ещё не реализованы — см. раздел «API» ниже. FN-27 (S1-08)
добавляет базовый пользовательский путь на React (`web/`): форма
режим/начало/длительность/период поиска/отсечение `as_of`, запуск расчёта
через реальный API с отслеживанием статуса, орбитальная сводка,
статусы источников с давностью, просмотр сохранённого результата и
демонстрационная панель по всем четырём фикстурам контракта — см. `web/README.md`.
FN-28 (S1-10) собирает текущие API/UI/SQLite в единое развёртывание (`Dockerfile`,
`compose.yaml`) и добавляет сквозную проверку этапа — см. раздел
«Развёртывание» ниже. FN-31 (S2-01, этап 2) добавляет отдельную линию
внешнего прогноза космопогоды — NOAA SWPC «3-Day Forecast», суточная
вероятность S1 и выше (`src/sources/noaa_3day_forecast.py`), и её
доменную интерпретацию для окна ВКД
(`src/domain/spaceweather/external_forecast.py`) — см. раздел «Внешний
прогноз NOAA 3-Day S1+» ниже; включает известное ограничение сессии
(парсер проверен на синтетических, не на реальных сохранённых фикстурах —
подробности там же и в `docs/method.md` §7.5). FN-36 (S2-06, этап 2)
реализует слой выгрузки (`src/export/`): машиночитаемый JSON и читаемый
HTML строятся только из одного уже сохранённого результата — см. раздел
«Выгрузка» ниже.

## Стек

Python 3.11+, FastAPI, Pydantic v2, [SGP4](https://pypi.org/project/sgp4/),
httpx, `jsonschema`/`referencing` (валидация результата по
`contracts/result.schema.json` перед сохранением, `src/api/service.py`),
[uv](https://docs.astral.sh/uv/) как менеджер пакетов и виртуальных
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
нет — источники, подключённые на этом этапе (NOAA SWPC, CelesTrak), не
требуют учётных данных; переменные Space-Track появятся вместе с
коннектором `space-track-gp-history`.
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
Success: no issues found in 25 source files

$ uv run pytest -v
tests/api/test_isolation.py::test_concurrent_calculations_do_not_mix_parameters_statuses_or_data PASSED
tests/api/test_isolation.py::test_concurrent_calculations_have_independent_task_status PASSED
tests/api/test_isolation.py::test_recompute_with_same_parameters_creates_a_new_immutable_result PASSED
tests/api/test_isolation.py::test_orbit_fetch_status_snapshot_is_not_mutated_by_a_later_concurrent_attempt PASSED
tests/api/test_isolation.py::test_orbit_fetch_success_after_a_prior_failure_does_not_inherit_the_old_error PASSED
tests/api/test_isolation.py::test_concurrent_swpc_success_and_failure_do_not_contaminate_each_others_result PASSED
tests/api/test_requests.py::test_create_calculation_returns_202_pending PASSED
tests/api/test_requests.py::test_get_calculation_status_unknown_task_is_404 PASSED
tests/api/test_requests.py::test_get_result_unknown_id_is_404 PASSED
tests/api/test_requests.py::test_current_mode_full_flow_returns_real_orbit_and_honest_mechanism_gaps PASSED
tests/api/test_requests.py::test_historical_modes_fail_with_clear_not_implemented[historical_analysis] PASSED
tests/api/test_requests.py::test_historical_modes_fail_with_clear_not_implemented[historical_forecast] PASSED
tests/api/test_requests.py::test_orbit_source_failure_fails_the_job_not_a_fake_success PASSED
tests/api/test_requests.py::test_orbit_source_failure_falls_back_to_last_stored_record PASSED
tests/api/test_requests.py::test_swpc_source_failure_does_not_fail_the_job PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides0] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides1] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides2] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides3] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides4] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides5] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides6] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides7] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides8] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides9] PASSED
tests/api/test_requests.py::test_invalid_requests_are_rejected_with_uniform_error_format[overrides10] PASSED
tests/api/test_requests.py::test_list_results_and_sources_status PASSED
tests/api/test_requests.py::test_refresh_sources_forces_a_fetch PASSED
tests/api/test_requests.py::test_swpc_failure_warning_fetch_attempt_id_is_traceable_in_the_log PASSED
tests/api/test_requests.py::test_negative_elements_age_is_clamped_to_non_negative_in_the_stored_result PASSED
tests/api/test_requests.py::test_unexpected_internal_error_returns_sanitized_message_not_raw_exception_text PASSED
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
135 passed, 1 skipped
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

### Внешний прогноз NOAA 3-Day S1+ (`src/sources/noaa_3day_forecast.py`, FN-31)

Отдельная от наблюдения GOES выше линия обязательного изменения №1
(main-prompt.md §11 «Прогноз»): суточная вероятность (0–100%, `percent`)
события уровня S1 и выше на каждый из трёх дней бюллетеня NOAA SWPC
«3-Day Forecast» (раздел B «Solar Radiation Storm Forecast»), отдельно
получаемая, сохраняемая и интерпретируемая от наблюдения (`pfu`,
`noaa-swpc-proton-flux`). Production-шлюз `fetch_and_store` (TTL, повторы,
429, заморозка/отключение) — по образцу `src/sources/swpc.py`; для
архивной линии — `fetch_and_store_archived_bulletin` (без TTL — архивный
ответ не устаревает). Доменная интерпретация для окна ВКД —
`src/domain/spaceweather/external_forecast.py`: показывает исходные
прогнозные дни и их пересечение с окном, никогда не делит вероятность по
часам, не умножает на длительность окна, не суммирует через полночь и не
называет её вероятностью ВКД. Версионирование по прогнозируемому
календарному дню даёт строгий `historical_forecast` (поздний выпуск после
`as_of` не виден) через уже существующее правило `select_as_of`, без
нового кода отсечения — подробности и доказательства на синтетических
фикстурах — `docs/method.md` §7.

**Подключено к расчётному API** (round 3 ревью PR #24 — «получение без
использования»): `src/api/service.py` получает живую линию в каждом
`mode=current` расчёте и в `/api/sources/refresh` (статус — в
`/api/sources/status`), выбирает пригодные записи через `select_as_of` и
передаёт их в `assess_external_forecast` для каждого окна — реальные
исходные прогнозные дни, их пересечение с окном и явная оговорка «не
вероятность ВКД» доходят до `mechanisms[*].notes`/`record_ids` (для
`space_weather`) и до `result.data_manifest`, когда прогноз пересекается с
окном. Эта суточная вероятность по-прежнему НЕ создаёт собственного
`max_level`/`exceedance_hours_by_level` (контракт не ограничивает
`notes`/`record_ids` статусом) — с FN-38 (ниже) `mechanisms[*].status` для
`space_weather` больше не всегда `not_implemented`: его выбирает отдельная
классификация НАБЛЮДЕНИЯ GOES. Тест сквозного пути —
`tests/api/test_requests.py::test_noaa_3day_forecast_is_used_in_windows_and_manifest_when_it_overlaps`.

### Наблюдаемый GOES S-классификатор — Механизм 1 (`src/domain/spaceweather/observed_classifier.py`, FN-38)

Вторая, независимая от суточного прогноза выше линия того же Механизма 1:
классифицирует уже получаемое наблюдение GOES pfu (`src/sources/swpc.py`,
FN-22) по шкале S NOAA (main-prompt.md §11: фон <10, S1>=10, S2>=100,
S3>=1000 pfu — дословно, происхождение — `sources.yaml` →
`space_weather[0].classification`, main-prompt.md §11, не подобрано этой
задачей). Подключено к `src/api/service.py`: каждый `mode=current` расчёт
выбирает наблюдения окна через новую `src.store.select_observed_range`
(не `select_as_of` — у этого источника `published_at` всегда `null`) и
строит реальный `mechanisms[*space_weather]` вместо заглушки —
`status="ok"` с настоящими `max_level`/`exceedance_hours_by_level`, когда
окно полностью покрыто пригодными отсчётами в пределах ожидаемого шага
между измерениями, и явные `missing_data`/`stale_data`/`source_error`/
`beyond_horizon` иначе (никогда не благоприятная оценка вместо пробела,
main-prompt.md §2). GOES — мгновенное наблюдение без собственного
горизонта прогноза вперёд: часть окна после текущего момента расчёта —
всегда `beyond_horizon`, не «спокойно» (main-prompt.md §4) — обоснованный
внешний прогноз минимум на 6 часов даёт отдельная линия NOAA 3-Day Forecast
выше. Подробности решения, таблица выбора статуса и известные ограничения
(геомагнитный модулятор Kp, шкала R — не подключены) — `docs/method.md` §8.
Тесты: `tests/domain/spaceweather/test_observed_classifier.py` (чистая
классификация, все уровни и границы), `tests/store/test_observed_range.py`
(выборка), `tests/api/test_observed_flux_integration.py` (сквозной путь
через `run_calculation` — ту же функцию, что вызывает HTTP-роутер).

**Известное ограничение этой сессии.** Сетевой доступ к
`services.swpc.noaa.gov`/`www.ngdc.noaa.gov` был заблокирован
egress-прокси песочницы (`403` на каждый проверенный хост) — парсер
проверен на синтетических, явно помеченных как синтетические фикстурах
(`tests/fixtures/sources/noaa_3day_forecast/synthetic/`), не на реальных
сохранённых ответах. Проверка на реальном текущем и историческом (включая
2024-05-10 12:30 UTC) выпуске и карта пробелов мая-июня 2024 по телам
реальных выпусков — открытый пункт, подробно — `docs/method.md` §7.5.

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
- **Исторический режим CelesTrak-путём.** `fetch_elements_for_request`
  поддерживает только `mode="current"`: для `historical_analysis`/
  `historical_forecast` поднимается `HistoricalElementsUnsupportedError` до
  какого-либо сетевого обращения — Space-Track `GP_HISTORY` остаётся
  задокументированным, но не реализованным опциональным источником (нет
  учётной записи команды, см. `sources.yaml`, продукт
  `space-track-gp-history`); современные элементы CelesTrak не
  подставляются вместо исторических ни при каких обстоятельствах
  (.ai/main-prompt.md §11 «Траектория»). Актуальный исторический путь —
  NASA OEM ниже.
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
- **Реестр источников `sources.yaml`.** Продукты `celestrak-gp` (текущий),
  `space-track-gp-history` (задокументирован, не реализован) и
  `nasa-iss-oem-history` (исторический, реализован — см. ниже) — с URL,
  единицами, охватом, частотой обновления, наличием `published_at` и
  пригодностью для строгого replay (.ai/main-prompt.md §7, §10).

### Исторические элементы: NASA TOPO CCSDS OEM (`src/sources/orbit_history.py`, `src/domain/orbit/interpolate.py`, FN-33)

Основной путь `historical_analysis`/`historical_forecast` (решение владельца
задачи, зафиксированное в `sources.yaml`, продукт `nasa-iss-oem-history`):
публичный, не требующий ключа бакет NASA JSC/FOD/TOPO
(`iss-coords/<дата>/ISS_OEM/ISS.OEM_J2K_EPH.txt`, CCSDS OEM 2.0 KVN) —
готовые векторы состояния (позиция км + скорость км/с в `REF_FRAME=EME2000`)
на неравномерной сетке узлов, не GP/TLE. Space-Track `GP_HISTORY` остаётся
опциональным, не реализованным источником (см. выше); OEM не подменяет его,
а полностью занимает его роль в этой версии сервиса.

- **Получение и разбор (`src/sources/orbit_history.py`).** `parse_oem`
  разбирает заголовок, единственный блок `META`, векторы состояния (строки
  `COMMENT` — масса/сопротивление, узлы, таблица манёвров — пропускаются, не
  парсятся); `parse_s3_listing` разбирает S3 `ListBucketResult` листинга
  выпуска (`xml.etree.ElementTree`, без новой зависимости).
  `verify_fetch_integrity` перепроверяет `*.meta.json` (заголовки исходного
  HTTP-ответа) против листинга и фактических байтов файла (sha256 тела,
  `ETag`/MD5, `Last-Modified`) — то, что
  `tests/fixtures/orbit/history/README.md` документирует как проверенное
  вручную, здесь исполняемый тест
  (`tests/orbit/test_orbit_history.py::test_verify_fetch_integrity_passes_for_real_fixtures`).
- **Время честности — суть задачи.** `published_at` — S3 `LastModified`
  датированного `.txt`-объекта из листинга, **никогда** заголовок
  `CREATION_DATE` (момент, когда баллистик создал файл — может
  предшествовать появлению файла в архиве на срок до нескольких суток;
  реальный случай — выпуск `2024-05-12`: `CREATION_DATE=2024-05-10T18:51:53Z`,
  `LastModified=2024-05-13T03:06:46Z`) и **никогда** имя датированной папки.
  Ограничение доказательства зафиксировано явно и программно
  (`orbit_history.PUBLICATION_EVIDENCE_NOTE`, записывается в
  `spatial_context` каждой записи): S3 `LastModified` доказывает время ЭТОЙ
  ВЕРСИИ объекта в бакете, а не аудированную историю публичной
  доступности/ACL бакета — формулировка «доказан точный момент публичного
  открытия» нигде не используется.
- **Два раздельных пути отбора** (main-prompt.md §1 «Последующие наблюдения
  — отдельная ветка кода»), оба в `src/sources/orbit_history.py`, вызываются
  через `src/sources/orbit.py::select_oem_elements_for_request` (актуальная
  точка входа для исторических режимов):
  - `select_release_for_forecast` — `historical_forecast`:
    `published_at <= as_of` И интервал `[USEABLE_START_TIME,
    USEABLE_STOP_TIME]` выпуска покрывает расчётный интервал целиком; среди
    пригодных — максимальный `published_at`. Ни при каких обстоятельствах
    не берётся более поздний или не покрывающий выпуск, и не современные
    элементы CelesTrak.
  - `select_release_for_analysis` — `historical_analysis`, НЕ фильтруется по
    `as_of`: предпочитается выпуск, созданный вскоре после интересующего
    момента (ближе к фактической траектории). Запись по этому пути всегда
    несёт `quality="reconstructed"`.
  - Если пригодного выпуска нет, `select_oem_elements_for_request` поднимает
    `HistoricalElementsUnsupportedError` — критический пробел архива, а не
    повод подставить более позднюю или современную геометрию.
- **Интерполяция, не SGP4 (`src/domain/orbit/interpolate.py`).** OEM не даёт
  среднеэлементного набора — `interpolate_state` восстанавливает
  положение/скорость между узлами кубической интерполяцией Эрмита по каждой
  оси (`INTERPOLATION_METHOD = "hermite-cubic-per-axis-v1"` — версионировано
  для `algorithm_version`, .ai/main-prompt.md §3), используя заданную в узле
  скорость как производную; узел несёт готовую производную, поэтому метод не
  требует оценки конечными разностями и не нуждается в новой зависимости
  (`scipy` не входит в `pyproject.toml`). Экстраполяция запрещена: запрос
  времени вне охваченного узлами интервала поднимает `InterpolationError`, а
  не подставляет ближайшее значение молча.
- **Точность интерполяции.** `tests/orbit/test_orbit_history.py` держит вне
  входа реальный узел плотного ~2-секундного участка манёвра
  (`2024-05-24T14:16`, `nasa_iss_oem_2024-05-12.txt`) и сверяет интерполянт
  с этим НЕ переданным на вход значением — не с повторным расчётом той же
  формулой (.ai/main-prompt.md §9, п.5).
- **Система координат** — `EME2000`, совпадает с системой радиантов MMOD
  (`src/domain/mmod`) — преобразование в TEME для этого источника не
  требуется (TEME — только у SGP4/TLE-пути выше).
- **Область задачи.** Гейт/проба (main-prompt.md §11, по аналогии с
  `src/sources/archive_probe.py`): парсинг и отбор работают над 4 реальными
  сохранёнными выпусками (`tests/fixtures/orbit/history/`, см. её README);
  production-шлюз с периодическим refresh/TTL и постраничной загрузкой
  полного архива в хранилище — задача следующего этапа.

## API (`src/api/`)

Расчётный API S1-07: пути и формы — `contracts/README.md`, раздел «Endpoint
shapes для S1-07»; тонкие роутеры — `src/api/routes.py`, вся логика —
`src/api/service.py`, реестр задач в процессе — `src/api/jobs.py`, форма
запроса и ответов — `src/api/schemas.py`.

- **`POST /api/calculations`** — валидирует тело по
  `contracts/request.schema.json` (плюс правила, не выражаемые JSON Schema:
  `as_of <= start_at`, обязательный исторический период, см.
  `src/api/schemas.py`) и сразу отвечает `202 {"task_id", "status": "pending"}`.
  Сам расчёт выполняется в отдельном потоке (`loop.run_in_executor`) — long
  расчёта здесь нет (SGP4 — миллисекунды, сеть к источникам — секунды),
  поэтому отдельный брокер очередей не нужен (.ai/main-prompt.md §6).
- **`GET /api/calculations/{task_id}`** — статус задачи
  (`pending`/`running`/`done`/`failed`), `result_id` при успехе,
  `{"code", "message"}` при отказе — без трассировок.
- **`GET /api/results/{result_id}`** / **`GET /api/results`** — сохранённый
  результат целиком или постранично урезанный список.
- **`GET /api/results/{result_id}/export.json`** / **`GET
  /api/results/{result_id}/export.html`** (FN-36, S2-06) — машиночитаемый
  JSON и читаемый HTML из этого же сохранённого результата, см. раздел
  «Выгрузка» ниже.
- **`POST /api/sources/refresh`** / **`GET /api/sources/status`** —
  принудительное обновление и статусы источников (`celestrak-gp`,
  `noaa-swpc-proton-flux`, `noaa-swpc-3day-forecast` — FN-31).

**Исходный объём этой задачи (S1-07) был «API и хранение, не интерпретация
механизмов»; FN-38 (S2-08) и FN-39 (S2-08) подключили реальную
интерпретацию обоих обязательных механизмов.**
`src/domain/spaceweather` реализует две независимые линии: уже упомянутый
`external_forecast.py` (FN-31: суточная вероятность S1+ NOAA 3-Day, без
собственного уровня/порога) и `observed_classifier.py` (FN-38:
классификация наблюдения GOES pfu по шкале S NOAA — подробности выше,
«Наблюдаемый GOES S-классификатор»). `src/domain/mmod` реализует и
геометро-кинематическую часть Механизма 2 (экранирование Землёй,
относительная скорость встречи, `effective_flux_ratio` — задача FN-32), и
нормировку к спорадическому фону (`ratio_to_background`, пороги 1.2/2,
источник — NASA MEO 2024 LEO forecast, задача FN-39,
`docs/mechanisms.md` §12). Поэтому в `mode=current` каждое окно несёт:

- `mechanisms[*mmod].status` — реальный вывод `ratio_to_background` (FN-39):
  `ok` с настоящими `max_level`/`exceedance_hours_by_level`, когда окно
  покрыто грид-узлами NASA MEO 2024 (2024-01-01..2025-01-01), иначе честные
  `missing_data`/`critical_gap=True` вне документа — никогда не имитирует
  спокойную обстановку за пределами покрытия (main-prompt.md §2). Геометрия
  станции (FN-32, `effective_flux_ratio`) намеренно НЕ перемножается с этим
  отношением — граница интеграции задокументирована в `docs/mechanisms.md`
  §12.3;
- `mechanisms[*space_weather].status` — реальный вывод классификации
  наблюдения (FN-38): `ok` с настоящими `max_level`/
  `exceedance_hours_by_level`, когда окно полностью покрыто пригодными
  отсчётами GOES, иначе явные `missing_data`/`stale_data`/`source_error`/
  `beyond_horizon` — никогда `not_implemented` для этого механизма с этой
  версии.

При этом:

- орбитальные элементы МКС реально получены с CelesTrak, сохранены в
  хранилище и участвуют в расчёте (SGP4-распространение по сетке текущего
  запроса) — `result.orbit` несёт реальные источник/эпоху/давность;
  `elements_epoch`/`is_reconstructed` не выдуманы;
  `result.data_manifest` содержит эту фактически использованную запись
  (main-prompt.md §3 «манифест собирается фактически использованными
  записями»);
  поток протонов NOAA SWPC при доступности тоже получается и сохраняется
  (виден в `source_status`) и **с FN-38 входит в манифест** (`record_kind:
  "observation"`) ровно тогда, когда реально попал в покрытый сегмент хотя
  бы одного окна — запись вне покрытия окна не создаёт видимость
  использования, которого не было;
- **суточная вероятность S1+ NOAA 3-Day Forecast (FN-31) тоже используется**,
  а не только получается: для каждого окна `assess_external_forecast`
  сопоставляет реально сохранённые (через `select_as_of`) прогнозные дни с
  границами окна, и результат — исходные дни, их пересечение с окном,
  явная оговорка «не вероятность ВКД» — попадает в
  `mechanisms[*space_weather].notes`/`record_ids` и в `result.data_manifest`,
  но по-прежнему НЕ создаёт собственного `max_level`/
  `exceedance_hours_by_level` (contracts/result.schema.json не ограничивает
  `notes`/`record_ids` статусом — О4 «видно, что прогнозировалось»
  реализуется независимо от статуса наблюдения);
- `recommendation.status` для `current` больше не гарантированно
  `all_windows_excluded`: с FN-38+FN-39 оба обязательных механизма могут
  быть `ok` одновременно, и тогда правило предпочтения окон (main-prompt.md
  §11, пп.1-4, `src/domain/windows`, FN-34/S2-04) реально сравнивает окна —
  доминирование (`selected`), конфликт/равенство/недостаточность оснований
  (`tie`/`insufficient_basis`), либо `all_windows_excluded`, когда хотя бы
  один механизм не покрыт для запрошенных дат в конкретном окне. Ни один из
  исходов не захардкожен в `src/api/service.py` — правило доминирования v2
  одинаково обрабатывает любое число реализованных механизмов;
- отказ источника потока протонов не роняет расчёт и не подменяется
  благоприятной оценкой (main-prompt.md §2, приёмка FN-26 «при отказе
  одного источника остальные доступны») — он остаётся виден в
  `source_status`/`warnings`, а орбита и форма результата не страдают;
  отказ источника орбитальных элементов сначала пробует последнюю
  сохранённую запись (main-prompt.md §5 «последний пригодный ответ
  сохраняется и отдаётся с явной давностью, когда источник недоступен») —
  геометрия при этом реальна, а её давность и, при необходимости,
  реконструкция видны честно (`orbit.elements_age_hours`/`is_reconstructed`,
  предупреждение `orbit-source-stale-fallback`); задача завершается
  ошибкой (`orbit_error_source`/`orbit_error_quota`/`orbit_error_corrupted`)
  только когда нет вообще ни живого, ни ранее сохранённого набора
  элементов — не пустым или придуманным результатом;
- `mode ∈ {historical_analysis, historical_forecast}` внутри поддерживаемого
  периода (1 мая — 30 июня 2024) сейчас всегда завершается понятной ошибкой
  задачи `historical_mode_not_implemented`: строгий исторический режим
  требует исторических орбитальных элементов (Space-Track `GP_HISTORY`),
  коннектор которых ещё не реализован (`src/sources/orbit.py`,
  `sources.yaml`), а современные элементы CelesTrak не подставляются вместо
  исторических ни при каких обстоятельствах (main-prompt.md §11
  «Траектория») — это осознанный `not_implemented`-отказ задачи, а не
  фиктивный успех;
- `lighting_constraint` в теле запроса отклоняется `422`: `src/domain/lighting`
  не реализован, а `contracts/result.schema.json` → `window.lighting.status`
  не имеет значения «не реализовано» (только
  `not_requested`/`satisfied`/`violated`) — придумать `satisfied` без
  проверки было бы тем самым ложным благоприятным выводом, который
  main-prompt.md §2 запрещает для отказов источников.

**Изоляция конкурентных запросов** (`tests/api/test_isolation.py`,
main-prompt.md §9 п.7, backend-prompt.md §2): каждая фоновая задача открывает
собственное соединение SQLite (не делит его с другими); реестр задач и
реестр статусов источников потокобезопасны и не хранят параметры расчёта в
модульных переменных — параметры и промежуточные данные передаются явно по
вызовам. Файл/схема хранилища создаются один раз при старте приложения
(`ensure_store_ready`), чтобы первый набор конкурентных запросов не гонялся
за созданием файла БД. `result.source_status` собирается из статуса,
построенного из СОБСТВЕННОГО исхода именно этой попытки обращения к
источнику (`OrbitFetchResult.status`/внутренний `_swpc_attempt_status`) — не
из общего реестра ни поздним повторным чтением, ни через возвращаемое
значение `registry.record_success`/`record_error`: тот само по себе строит
объект из ТЕКУЩЕГО общего состояния (`dataclasses.replace`) и не стирает
поля ошибки, унаследованные от чужой конкурентной попытки на том же
`source_id` — значит даже «снимок в момент вызова» мог быть загрязнён.
Общий реестр остаётся источником только для `/sources/status`/
`/sources/refresh` (глобальный, не привязанный к одному расчёту статус) и
для исходов `skipped_*` (когда сама попытка не делала живого обращения и
показывать нечего, кроме последнего известного состояния источника). См.
`test_orbit_fetch_status_snapshot_is_not_mutated_by_a_later_concurrent_attempt`,
`test_orbit_fetch_success_after_a_prior_failure_does_not_inherit_the_old_error`,
`test_concurrent_swpc_success_and_failure_do_not_contaminate_each_others_result`
(последний — синхронизированный через `threading.Barrier`, чтобы обе
попытки гарантированно пересеклись по времени).

**Прослеживаемость и защита контракта.** Каждое предупреждение с
`fetch_attempt_id` доказуемо: тот же id попадает в структурированную запись
лога вместе со сквозными `task_id`/`result_id` (`src/api/service.py:_log`) —
по нему восстанавливается конкретная попытка, а не только текст без следа.
Полностью собранный результат валидируется по
`contracts/result.schema.json` (пакеты `jsonschema`/`referencing`) прямо
перед сохранением — контрактное нарушение (например давность элементов
`< 0`, что `orbit.elements_age_hours` не пропускает — отрицательное значение
приводится к модулю, main-prompt.md §2 «пропуск не заменяется нулём», тот же
принцип к знаку) останавливает сохранение явной ошибкой, а не уходит
клиенту как «корректный» результат. Текст любого НЕПРЕДВИДЕННОГО исключения
(в отличие от curated `CalculationError`/`SwpcFormatError`/`OrbitSourceError`
и т.п. — их текст уже осознанно информативен и без секретов) не
возвращается клиенту как есть и не пишется в лог как есть: он мог бы
раскрыть путь к БД, URL с ключом в query-строке или другую внутреннюю
деталь (.ai/backend-prompt.md §4 «маскирование на уровне логгера, а не на
уровне дисциплины»). Единая функция `sanitize_unexpected_error` заменяет
такой текст на имя класса исключения — и в ответе клиенту (с `task_id` для
сопоставления), и в структурированном логе, и в общем реестре статусов
источников.

## Выгрузка (`src/export/`, FN-36)

Машиночитаемый JSON и читаемый HTML строятся **только** из одного уже
сохранённого immutable-результата — `src/api/routes.py` читает его тем же
запросом (`_get_stored_result_or_404`), что и `GET /api/results/{result_id}`,
и передаёт этот же объект обеим функциям выгрузки. `src/export/` не ходит в
сеть, не выбирает записи в хранилище и не пересчитывает домен — единственный
вход это уже собранный `dict` в форме `contracts/result.schema.json`
(`.ai/main-prompt.md` §3 «интерфейс и оба формата выгрузки читают один и тот
же сохранённый объект»; граница закреплена автоматической проверкой
импортов — `tests/export/test_boundaries.py`).

- **`GET /api/results/{result_id}/export.json`** — `src/export/json_export.py`
  проверяет форму сохранённого результата по `contracts/result.schema.json`
  (`src/export/_validate.py`) и возвращает его независимую JSON-копию —
  семантически равную тому, что отдал бы `GET /api/results/{result_id}` в
  этот же момент (без добавленных, убранных или переупорядоченных полей).
- **`GET /api/results/{result_id}/export.html`** — `src/export/html_export.py`
  строит самодостаточную HTML-страницу с той же терминологией, что и
  `web/src/components` (`labels.ts`): запрос и `mode`/`as_of`; оба окна
  одинаковой длительности; раздельные оценки по механизмам
  (`space_weather`/`mmod`) с их единицами (pfu-пороги S1/S2/S3, безразмерное
  отношение к фону elevated/pronounced); `coverage`/архивные пробелы;
  рекомендация или причина её отсутствия; орбита (источник, эпоха, давность,
  признак реконструкции); `data_manifest`; статусы источников;
  `algorithm_version`; ограничения и предупреждения. Все времена печатаются
  явно в UTC (`_format_utc` отказывает, если время результата пришло без
  явного смещения — main-prompt.md §1); каждое `null`-значение показывает
  причину его отсутствия рядом (`_null_span`), а не пустую ячейку; шаблон не
  добавляет собственных формулировок вроде «безопасно» — текст
  предупреждений/пояснений/рекомендации выводится ровно так, как хранится в
  результате (main-prompt.md §4, §12).
- **Неизвестный `result_id`** — тот же `404 {"error": {"code":
  "result_not_found", ...}}`, что и у `GET /api/results/{result_id}`: оба
  формата выгрузки используют один и тот же вызов чтения хранилища.
- **Повреждённый/невалидный сохранённый payload** (например будущий дрейф
  контракта без изменения уже записанных строк SQLite) не превращается в
  правдоподобно выглядящий отчёт: обе функции сначала проверяют результат по
  `contracts/result.schema.json` и поднимают `ExportError` при нарушении —
  роутер отвечает `500 {"error": {"code": "result_export_failed", ...}}`, а
  не отдаёт частично собранный HTML/JSON.
- **Детерминизм.** Ни одна из функций не мутирует переданный результат;
  повторный вызов с тем же сохранённым объектом даёт побайтово идентичный
  HTML и структурно идентичный JSON — `result_id` и данные не меняются
  (`tests/export/`, `tests/api/test_export.py`).

## Структура проекта

Полное описание слоёв и границ между ними — `.ai/main-prompt.md`, §8.

```text
src/
├── api/        # FastAPI: app.py (сборка), routes.py (тонкие роутеры),
│               # service.py (оркестрация), jobs.py (реестр задач), schemas.py
├── config.py   # Настройки из env, без секретов
├── sources/    # Получение и нормализация — http.py/swpc.py/status.py (NOAA SWPC),
│               # orbit.py (CelesTrak GP/TLE), archive_probe.py (архивы DONKI/SWPC),
│               # noaa_3day_forecast.py (NOAA 3-Day Forecast S1+, FN-31)
├── store/      # Хранение, версии, выборка по as_of — schema.py/records.py/results.py
├── domain/     # Расчёты: spaceweather (external_forecast.py — FN-31), orbit (propagate.py —
│               # SGP4), mmod, lighting, windows
└── export/     # HTML и JSON из одного сохранённого результата (json_export.py,
                # html_export.py, _validate.py — FN-36)
sources.yaml    # Реестр источников (main-prompt.md §7): space_weather (noaa-swpc-proton-flux,
                # nasa-donki-notifications, noaa-swpc-forecast-discussion-archive,
                # noaa-swpc-3day-forecast[-archive]),
                # sources (celestrak-gp, space-track-gp-history, MMOD)
docs/
├── mechanisms.md  # Обоснование MMOD (FN-25)
└── method.md      # Пригодность архивов космопогоды для строгого replay (FN-23);
                   # §7 — NOAA 3-Day Forecast S1+ (FN-31)
tests/
├── api/        # test_requests.py, test_isolation.py (S1-07), test_export.py (FN-36)
├── sources/    # test_swpc.py, test_archive_publication.py, test_noaa_3day_forecast.py +
│               # fixtures/sources/{swpc,archive}/ (сохранённые реальные ответы),
│               # fixtures/sources/noaa_3day_forecast/synthetic/ (явно синтетические, FN-31)
├── domain/spaceweather/  # test_external_forecast.py (FN-31)
├── export/     # test_json_export.py, test_html_export.py, test_boundaries.py (FN-36)
├── orbit/      # test_propagation.py
├── fixtures/orbit/  # TLE-фикстуры и независимый эталон SGP4 verification
├── store/      # test_versions.py, test_as_of.py
└── test_health.py
```

Слой `web/` (React + TypeScript + Vite) — зона ответственности 4, описан в
`web/README.md`; вне объёма этой (backend) задачи.

## Развёртывание (FN-28, S1-10)

Одна команда поднимает уже существующие слои — API (`src/api/`), UI
(`web/`) и постоянное хранилище SQLite — как единый стек: `Dockerfile`
(цели `api`, `web` — см. комментарии в файле) и `compose.yaml` не добавляют
новой логики, только упаковывают то, что описано в разделах выше
(.ai/main-prompt.md §8: контейнеры — упаковка, не слой сервиса).

### Запуск

```bash
docker compose up --build -d
docker compose ps          # оба сервиса: healthy
```

- UI: `http://localhost:8080`
- API напрямую: `http://localhost:8000` (UI обращается к нему через
  `http://localhost:8080/api/...` — реверс-прокси `docker/nginx.conf`, тот
  же относительный путь `/api`, что и у `npm run dev`, см. `web/README.md`)
- health: `curl -fsS http://localhost:8000/health`

Секретов на этом этапе нет (`APP_ENV`/`LOG_LEVEL`/пути хранилища заданы
прямо в `compose.yaml` — main-prompt.md §7 «пороги, адреса — в конфиге»;
переменные Space-Track появятся вместе с историческим коннектором и тогда
же попадут в `.env.example`/секреты окружения, а не в `compose.yaml`).

### Постоянство: перезапуск не теряет результат

`api-data` — именованный том, смонтированный в `/app/data` (тот же путь,
что и `STORE_DB_PATH=data/store.sqlite3`/`STORE_RAW_DIR=data/raw` по
умолчанию, `src/config.py`):

```bash
# 1. посчитать что-нибудь (см. «Сквозная проверка» ниже) и запомнить result_id
# 2. перезапустить только api, не трогая том
docker compose restart api
docker compose ps          # снова healthy
curl -fsS http://localhost:8000/api/results/<result_id>   # тот же результат
```

### Изоляция и отказ источника — без пересборки

Уже реализованное поведение (FN-22/FN-26, `tests/api/test_isolation.py`,
`src/sources/status.py`) остаётся видимым и через развёрнутый стек:

- **два запроса не смешиваются** — `POST /api/calculations` дважды подряд с
  разными параметрами даёт независимые `task_id`/`result_id` (сквозной тест
  ниже это проверяет).
- **отключение источника** — документированный статический переключатель:
  `enabled: false` у нужного источника в `sources.yaml`, без пересборки
  образа (файл читается заново при каждом обращении); отдаётся как
  `frozen: true` в `GET /api/sources/status`/`result.source_status` — не
  благоприятная оценка, а видимая заморозка (main-prompt.md §5, §7).
- **отказ источника** — недоступность CelesTrak/NOAA SWPC даёт понятный
  `job.status = "failed"` с кодом ошибки (например `orbit_error_source`) или
  предупреждение в `source_status`, никогда молчаливый «успех» (см. раздел
  «API» выше).

### Сквозная проверка этапа

```bash
uv sync --locked --all-groups   # если ещё не выполнялось — тесты запускаются с хоста, не из контейнера
STAGE1_BASE_URL=http://localhost:8000 uv run pytest tests/integration/test_stage1.py -m integration -v
```

Проверяет `tests/integration/test_stage1.py`: health; полный путь
`mode=current` (API создаёт задание → получает реальные CelesTrak/NOAA
SWPC → считает орбиту SGP4 → сохраняет результат) с формой ответа,
соответствующей контракту; персистентность результата через
`docker compose restart api` (`STAGE1_COMPOSE_CMD`, по умолчанию
`docker compose`); изоляция двух одновременных запросов; форма
`/api/sources/status`. Если реальный источник недоступен из сети, в которой
запущена проверка, соответствующие проверки помечаются `SKIPPED` с точной
причиной (`pytest.skip`) — это не сбой пайплайна, а честный сигнал «источник
недоступен отсюда» (см. «Известное ограничение окружения сборки» ниже) —
но `job.status` в этом случае обязан быть `failed`, что тест и проверяет
первым делом, прежде чем что-либо пропустить.

UI-путь той же проверки — `web/tests/stage1.spec.ts` (Playwright, реальный
Chromium, не jsdom):

```bash
cd web
npm ci
STAGE1_WEB_BASE_URL=http://localhost:8080 npx playwright install --with-deps chromium   # один раз
STAGE1_WEB_BASE_URL=http://localhost:8080 npm run test:e2e
```

Открывает UI, задаёт `mode=current`, нажимает «Рассчитать», ждёт результат
или честную ошибку источника, проверяет панель источников и совпадение
результата в «Сохранённые результаты» с только что рассчитанным (тот же
принцип «интерфейс и выгрузка читают один и тот же сохранённый объект»,
main-prompt.md §3).

### Известное ограничение окружения сборки

В песочнице, где готовился этот PR, исходящая сеть разрешена только к
пакетным реестрам (PyPI, npm) — не к Docker Hub и не к CelesTrak/NOAA SWPC
(организационная политика egress-прокси блокирует оба класса хостов кодом
403). Из-за этого в этой среде удалось проверить:

- `uv run ruff check .` / `uv run mypy src` / `uv run pytest` (без сети,
  как и раньше) — зелёные (полный вывод — раздел «Проверки» выше);
- `docker compose config` — `compose.yaml` валиден;
- `tests/integration/test_stage1.py` и `web/tests/stage1.spec.ts` — против
  сервиса, запущенного напрямую (`uv run python -m src.api.app` +
  `npm run dev`, без Docker): health, изоляция и форма статусов источников
  проходят; проверка `mode=current` корректно распознаёт недоступность
  CelesTrak/NOAA SWPC как честный отказ (`job.status == "failed"`,
  `test.skip`/`pytest.skip` с точной причиной, не имитация успеха) —
  именно то поведение, которое main-prompt.md §2 требует от отказа
  источника.

Не проверено в этой среде (не удалось из-за политики egress, а не из-за
ошибки в конфигурации) и остаётся сделать тому, кто воспроизводит запуск с
обычным доступом в интернет: сборка образов `docker build`/`docker compose
up --build` (нужен Docker Hub — `python:3.11-slim`, `node:22-slim`,
`nginx:1.27-alpine`) и получение реального источника внутри собранного
стека (нужны CelesTrak/NOAA SWPC). Приёмка FN-28 «другой участник
воспроизводит запуск по README» подразумевает именно это — обычную сеть
разработчика, которой здесь не было.

### Доступ жюри

Согласованной площадки и секретов для внешнего (публичного) развёртывания
на этом этапе нет — команда не покупала хостинг ради одной задачи каркаса.
Внешний deploy этой задачей осознанно **не выполняется**: этот README не
выдаёт `http://localhost:8080` за URL, доступный жюри, — это адрес
локального стека, поднятого по инструкции выше на машине того, кто его
запустил. Публичный доступный URL — предмет отдельной задачи, когда
согласованная площадка и секреты появятся.
