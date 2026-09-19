/**
 * UI-only demonstration `CalculationResult` objects for two states the four
 * canonical contract fixtures (`web/src/api/fixtures.ts`) do not cover:
 * a genuine mechanism conflict (`insufficient_basis` caused by rule 3 of
 * .ai/main-prompt.md §11 "Правило предпочтения окон v2" — one window better
 * on space_weather, the other better on mmod, so neither dominates) and a
 * mechanism assessment with `status === "beyond_horizon"` (window interval
 * outside the forecast horizon — "не покрыто", never "спокойно").
 *
 * These are hand-written, schema-shaped objects (mirroring
 * `web/src/api/types.ts` / `contracts/result.schema.json`), NOT backend
 * fixtures: they intentionally stay out of `contracts/fixtures/` and out of
 * `DEMO_FIXTURES` in `web/src/api/fixtures.ts`, which is contractually
 * locked to exactly the 4 backend-synced fixtures. They exist only to drive
 * `web/tests/uiStates.spec.ts` through mocked HTTP against the real
 * CalculationPanel, per FN-35.
 */
import type { CalculationResult } from '../../src/api/types'

export const conflictResult: CalculationResult = {
  result_id: 'res-ui-conflict-0001',
  computed_at: '2026-09-19T10:00:00Z',
  request: {
    mode: 'current',
    start_at: '2026-09-19T12:00:00Z',
    duration_hours: 4,
    search_window_hours: 8,
  },
  mode: 'current',
  as_of: null,
  algorithm_version: '0.2.0',
  data_manifest: [
    {
      record_id: 'rec-conflict-orbit-0001',
      source_id: 'celestrak-gp',
      source_version: '2026-09-19T08:00:00Z',
      record_kind: 'orbital_elements',
    },
    {
      record_id: 'rec-conflict-swx-0001',
      source_id: 'noaa-swpc-proton-flux',
      source_version: '2026-09-19T09:45:00Z',
      record_kind: 'observation',
    },
    {
      record_id: 'rec-conflict-mmod-0001',
      source_id: 'meteor-stream-static-ref',
      source_version: '2024.1',
      record_kind: 'observation',
    },
  ],
  orbit: {
    source: 'celestrak',
    norad_id: '25544',
    elements_epoch: '2026-09-19T08:00:00Z',
    elements_age_hours: 2.0,
    coordinate_system: 'TEME',
    is_reconstructed: false,
    record_id: 'rec-conflict-orbit-0001',
  },
  windows: [
    {
      window_id: 'win-a',
      start_at: '2026-09-19T12:00:00Z',
      end_at: '2026-09-19T16:00:00Z',
      duration_hours: 4,
      mechanisms: [
        {
          mechanism: 'space_weather',
          status: 'ok',
          max_level: 'S1',
          exceedance_hours_by_level: { S1: 2.0, S2: 0, S3: 0 },
          coverage_fraction: 1.0,
          critical_gap: false,
          notes: [],
          record_ids: ['rec-conflict-swx-0001'],
        },
        {
          mechanism: 'mmod',
          status: 'ok',
          max_level: 'pronounced',
          exceedance_hours_by_level: { elevated: 3.0, pronounced: 1.5 },
          coverage_fraction: 1.0,
          critical_gap: false,
          notes: [
            'Оценка ведётся в относительных единицах к спорадическому фону — абсолютная калибровка источника недоступна.',
          ],
          record_ids: ['rec-conflict-mmod-0001', 'rec-conflict-orbit-0001'],
        },
      ],
      lighting: { requested: false, status: 'not_requested', note: null },
      excluded_from_comparison: false,
      exclusion_reason: null,
    },
    {
      window_id: 'win-b',
      start_at: '2026-09-19T15:00:00Z',
      end_at: '2026-09-19T19:00:00Z',
      duration_hours: 4,
      mechanisms: [
        {
          mechanism: 'space_weather',
          status: 'ok',
          max_level: 'S2',
          exceedance_hours_by_level: { S1: 3.0, S2: 1.0, S3: 0 },
          coverage_fraction: 1.0,
          critical_gap: false,
          notes: [],
          record_ids: ['rec-conflict-swx-0001'],
        },
        {
          mechanism: 'mmod',
          status: 'ok',
          max_level: 'background',
          exceedance_hours_by_level: { elevated: 0, pronounced: 0 },
          coverage_fraction: 1.0,
          critical_gap: false,
          notes: [
            'Оценка ведётся в относительных единицах к спорадическому фону — абсолютная калибровка источника недоступна.',
          ],
          record_ids: ['rec-conflict-mmod-0001', 'rec-conflict-orbit-0001'],
        },
      ],
      lighting: { requested: false, status: 'not_requested', note: null },
      excluded_from_comparison: false,
      exclusion_reason: null,
    },
  ],
  coverage: { requested_period_supported: true, archive_gaps: [] },
  limitations: [
    'Оценка MMOD ведётся в относительных единицах к фону, абсолютная калибровка потока не выполнена.',
    'Демонстрационный UI-объект (web/tests/fixtures/uiStates.ts), не результат расчёта сервиса.',
  ],
  warnings: [
    {
      code: 'mechanism-conflict',
      severity: 'advisory',
      mechanism: null,
      message:
        'Окно A ниже по механизму 1 (S1 против S2 у окна B), но выше по механизму 2 (выраженный уровень против фона у окна B) — окно B наоборот: ниже по механизму 2, но выше по механизму 1. Конфликт между механизмами: ни одно окно не доминирует.',
      record_ids: ['rec-conflict-swx-0001', 'rec-conflict-mmod-0001'],
      fetch_attempt_id: null,
      window_id: null,
    },
  ],
  recommendation: {
    status: 'insufficient_basis',
    window_id: null,
    explanation:
      'Автоматической рекомендации нет: окна A и B сравнимы (по обоим механизмам status = ok у обеих сторон), но конфликтуют между собой (FN-34/§11 правило 3) — окно A не хуже и устойчиво лучше окна B по механизму 1 (максимальный уровень S1 против S2, 2.0 против 3.0/1.0/0 часов превышения), однако окно B не хуже и устойчиво лучше окна A по механизму 2 (фон против выраженного уровня, 0/0 против 3.0/1.5 часов превышения). Ни одно окно не доминирует над другим ни по одному, ни по всем показателям сразу — это конфликт механизмов, а не равенство: показатели различаются, а не совпадают. Выбор между окнами остаётся за аналитиком.',
  },
  source_status: [
    {
      source_id: 'celestrak-gp',
      last_success_at: '2026-09-19T08:05:00Z',
      last_error_at: null,
      last_error_message: null,
      frozen: false,
      quota_limited: false,
    },
    {
      source_id: 'noaa-swpc-proton-flux',
      last_success_at: '2026-09-19T09:50:00Z',
      last_error_at: null,
      last_error_message: null,
      frozen: false,
      quota_limited: false,
    },
    {
      source_id: 'meteor-stream-static-ref',
      last_success_at: '2024-01-01T00:00:00Z',
      last_error_at: null,
      last_error_message: null,
      frozen: false,
      quota_limited: false,
    },
  ],
}

export const beyondHorizonResult: CalculationResult = {
  result_id: 'res-ui-beyond-horizon-0001',
  computed_at: '2026-09-19T10:00:00Z',
  request: {
    mode: 'current',
    start_at: '2026-09-19T12:00:00Z',
    duration_hours: 3,
    search_window_hours: 12,
  },
  mode: 'current',
  as_of: null,
  algorithm_version: '0.2.0',
  data_manifest: [
    {
      record_id: 'rec-horizon-orbit-0001',
      source_id: 'celestrak-gp',
      source_version: '2026-09-19T08:00:00Z',
      record_kind: 'orbital_elements',
    },
    {
      record_id: 'rec-horizon-swx-0001',
      source_id: 'noaa-swpc-proton-flux',
      source_version: '2026-09-19T09:45:00Z',
      record_kind: 'forecast',
    },
    {
      record_id: 'rec-horizon-mmod-0001',
      source_id: 'meteor-stream-static-ref',
      source_version: '2024.1',
      record_kind: 'observation',
    },
  ],
  orbit: {
    source: 'celestrak',
    norad_id: '25544',
    elements_epoch: '2026-09-19T08:00:00Z',
    elements_age_hours: 2.0,
    coordinate_system: 'TEME',
    is_reconstructed: false,
    record_id: 'rec-horizon-orbit-0001',
  },
  windows: [
    {
      window_id: 'win-a',
      start_at: '2026-09-19T12:00:00Z',
      end_at: '2026-09-19T15:00:00Z',
      duration_hours: 3,
      mechanisms: [
        {
          mechanism: 'space_weather',
          status: 'ok',
          max_level: 'background',
          exceedance_hours_by_level: { S1: 0, S2: 0, S3: 0 },
          coverage_fraction: 1.0,
          critical_gap: false,
          notes: [
            'Окно целиком внутри горизонта прогноза механизма 1 (не более 6 часов вперёд от момента расчёта).',
          ],
          record_ids: ['rec-horizon-swx-0001'],
        },
        {
          mechanism: 'mmod',
          status: 'ok',
          max_level: 'background',
          exceedance_hours_by_level: { elevated: 0, pronounced: 0 },
          coverage_fraction: 1.0,
          critical_gap: false,
          notes: [
            'Оценка ведётся в относительных единицах к спорадическому фону — абсолютная калибровка источника недоступна.',
          ],
          record_ids: ['rec-horizon-mmod-0001', 'rec-horizon-orbit-0001'],
        },
      ],
      lighting: { requested: false, status: 'not_requested', note: null },
      excluded_from_comparison: false,
      exclusion_reason: null,
    },
    {
      window_id: 'win-b',
      start_at: '2026-09-19T22:00:00Z',
      end_at: '2026-09-20T01:00:00Z',
      duration_hours: 3,
      mechanisms: [
        {
          mechanism: 'space_weather',
          status: 'beyond_horizon',
          max_level: null,
          exceedance_hours_by_level: null,
          coverage_fraction: 0.0,
          critical_gap: true,
          notes: [
            'Окно начинается через 12 часов от момента расчёта — прогноз механизма 1 покрывает не более 6 часов вперёд. Интервал вне горизонта — «не покрыто», а не «спокойно» (.ai/main-prompt.md §4).',
          ],
          record_ids: [],
        },
        {
          mechanism: 'mmod',
          status: 'ok',
          max_level: 'background',
          exceedance_hours_by_level: { elevated: 0, pronounced: 0 },
          coverage_fraction: 1.0,
          critical_gap: false,
          notes: [
            'Оценка ведётся в относительных единицах к спорадическому фону — абсолютная калибровка источника недоступна.',
          ],
          record_ids: ['rec-horizon-mmod-0001', 'rec-horizon-orbit-0001'],
        },
      ],
      lighting: { requested: false, status: 'not_requested', note: null },
      excluded_from_comparison: true,
      exclusion_reason:
        'Критический пробел по механизму 1: интервал окна вне горизонта прогноза (не покрыто) — трактуется как «оценить невозможно», а не как основание для авто-победы окна A.',
    },
  ],
  coverage: { requested_period_supported: true, archive_gaps: [] },
  limitations: [
    'Демонстрационный UI-объект (web/tests/fixtures/uiStates.ts), не результат расчёта сервиса.',
  ],
  warnings: [
    {
      code: 'beyond-forecast-horizon',
      severity: 'advisory',
      mechanism: 'space_weather',
      message:
        'Окно B начинается через 12 часов от момента расчёта — за пределами 6-часового горизонта прогноза механизма 1. Это «не покрыто», а не «спокойно».',
      record_ids: ['rec-horizon-swx-0001'],
      fetch_attempt_id: null,
      window_id: 'win-b',
    },
  ],
  recommendation: {
    status: 'selected',
    window_id: 'win-a',
    explanation:
      'Окно B исключено из сравнения правилом доминирования v2 (§11 правило 1): интервал окна вне горизонта прогноза механизма 1 — критический пробел, а не проигрыш по баллам. Окно A остаётся единственным окном без критического пробела и рекомендуется.',
  },
  source_status: [
    {
      source_id: 'celestrak-gp',
      last_success_at: '2026-09-19T08:05:00Z',
      last_error_at: null,
      last_error_message: null,
      frozen: false,
      quota_limited: false,
    },
    {
      source_id: 'noaa-swpc-proton-flux',
      last_success_at: '2026-09-19T09:50:00Z',
      last_error_at: null,
      last_error_message: null,
      frozen: false,
      quota_limited: false,
    },
    {
      source_id: 'meteor-stream-static-ref',
      last_success_at: '2024-01-01T00:00:00Z',
      last_error_at: null,
      last_error_message: null,
      frozen: false,
      quota_limited: false,
    },
  ],
}
