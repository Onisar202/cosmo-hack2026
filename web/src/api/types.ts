/**
 * TypeScript-зеркало `contracts/{request,result}.schema.json`. Источник
 * истины о форме — JSON Schema в `contracts/`; этот файл не должен
 * расходиться с ней (contracts/README.md, .ai/main-prompt.md §10 п.3).
 */

export type Mode = 'current' | 'historical_analysis' | 'historical_forecast'

export interface LightingConstraint {
  requires_sunlight: boolean
}

export interface CalculationRequest {
  mode: Mode
  start_at: string
  duration_hours: number
  search_window_hours: number
  as_of?: string
  lighting_constraint?: LightingConstraint
}

export type RecordKind = 'observation' | 'forecast' | 'warning' | 'orbital_elements'

export interface DataManifestEntry {
  record_id: string
  source_id: string
  source_version: string
  record_kind: RecordKind
}

export interface OrbitSummary {
  // nasa-iss-oem-history (FN-41) — исторические эфемериды NASA TOPO CCSDS OEM:
  // единственный источник орбиты в historical_* режимах; current-элементы
  // CelesTrak туда не подставляются (contracts/result.schema.json).
  source: 'celestrak' | 'space-track' | 'nasa-iss-oem-history'
  norad_id: string
  elements_epoch: string
  elements_age_hours: number
  coordinate_system: string
  is_reconstructed: boolean
  record_id: string
}

export type Mechanism = 'space_weather' | 'mmod'

export type MechanismStatus =
  | 'ok'
  | 'not_implemented'
  | 'missing_data'
  | 'stale_data'
  | 'source_error'
  | 'beyond_horizon'
  // FN-41: событие подтверждено качественно (event_state = EVENT_PRESENT),
  // но уровень/длительность по доступной архивной линии не восстанавливаются.
  | 'qualitative_only'

/**
 * FN-41: три различимых состояния архивной событийной линии Механизма 1 —
 * «воздействие не выявлено» отличается от «оценить невозможно»
 * (contracts/result.schema.json, spaceWeatherEventState). NOT_APPLICABLE —
 * линия не применялась (механизм mmod, режим current).
 */
export type SpaceWeatherEventState =
  'EVENT_PRESENT' | 'NO_EVENT_DETECTED' | 'INSUFFICIENT_DATA' | 'NOT_APPLICABLE'

/**
 * FN-41: согласие ИСТОЧНИКОВ внутри одного механизма — не то же самое, что
 * расхождение МЕХАНИЗМОВ между собой (последнее разбирает правило
 * предпочтения окон). INSUFFICIENT_DATA — третье состояние «сравнивать
 * нечего», а не «источники согласны» (contracts/result.schema.json,
 * sourceAgreement).
 */
export type SourceAgreement = 'CONSISTENT' | 'CONFLICT' | 'INSUFFICIENT_DATA'

/** Оценка ОДНОЙ линии данных внутри механизма, сохранённая как есть. */
export interface SourceLineAssessment {
  source_id: string
  event_state: SpaceWeatherEventState
  coverage_fraction: number
  beyond_horizon: boolean
  record_ids: string[]
  notes: string[]
}

export interface SpaceWeatherExceedance {
  S1: number
  S2: number
  S3: number
}

export interface MmodExceedance {
  elevated: number
  pronounced: number
}

export interface MechanismAssessment {
  mechanism: Mechanism
  status: MechanismStatus
  /**
   * FN-41: обязательное поле контракта. В TypeScript — необязательное
   * намеренно: хранилище неизменяемо и продолжает отдавать результаты,
   * сохранённые до FN-41, у которых этого поля нет (main-prompt.md §3).
   * Интерфейс показывает строку только когда состояние действительно есть,
   * а не подставляет значение по умолчанию.
   */
  event_state?: SpaceWeatherEventState
  /** FN-41, та же оговорка о старых сохранённых результатах, что и выше. */
  source_agreement?: SourceAgreement
  /** FN-41: оценка каждой линии механизма отдельно, ни одна не стирается. */
  source_assessments?: SourceLineAssessment[]
  max_level: string | null
  exceedance_hours_by_level: SpaceWeatherExceedance | MmodExceedance | null
  coverage_fraction: number
  critical_gap: boolean
  notes: string[]
  record_ids: string[]
}

export type LightingStatus = 'not_requested' | 'satisfied' | 'violated'

export interface WindowLighting {
  requested: boolean
  status: LightingStatus
  note: string | null
}

export interface CalcWindow {
  window_id: string
  start_at: string
  end_at: string
  duration_hours: number
  mechanisms: MechanismAssessment[]
  lighting: WindowLighting
  excluded_from_comparison: boolean
  exclusion_reason: string | null
}

export interface ArchiveGap {
  source_id: string
  gap_start: string
  gap_end: string
  note: string
}

export interface Coverage {
  requested_period_supported: boolean
  archive_gaps: ArchiveGap[]
}

export type WarningSeverity = 'info' | 'advisory' | 'critical'

export type WarningMechanism = Mechanism | 'lighting' | 'orbit' | 'source' | null

export interface CalculationWarning {
  code: string
  severity: WarningSeverity
  mechanism: WarningMechanism
  message: string
  record_ids: string[]
  fetch_attempt_id: string | null
  window_id: string | null
}

export type RecommendationStatus =
  'selected' | 'tie' | 'insufficient_basis' | 'all_windows_excluded'

export interface Recommendation {
  status: RecommendationStatus
  window_id: string | null
  explanation: string
}

export interface SourceStatus {
  source_id: string
  last_success_at: string | null
  last_error_at: string | null
  last_error_message: string | null
  frozen: boolean
  quota_limited: boolean
}

export interface CalculationResult {
  result_id: string
  computed_at: string
  request: CalculationRequest
  mode: Mode
  as_of: string | null
  algorithm_version: string
  data_manifest: DataManifestEntry[]
  orbit: OrbitSummary
  windows: CalcWindow[]
  coverage: Coverage
  limitations: string[]
  warnings: CalculationWarning[]
  recommendation: Recommendation
  source_status: SourceStatus[]
}

export type JobStatus = 'pending' | 'running' | 'done' | 'failed'

export interface ApiErrorBody {
  code: string
  message: string
}

export interface TaskCreatedResponse {
  task_id: string
  status: 'pending'
}

export interface TaskStatusResponse {
  task_id: string
  status: JobStatus
  result_id: string | null
  error: ApiErrorBody | null
}

export interface ResultListItem {
  result_id: string
  computed_at: string
  mode: Mode
  as_of: string | null
  recommendation_status: string
}
