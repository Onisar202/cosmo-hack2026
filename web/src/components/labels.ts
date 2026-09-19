/**
 * Текстовые подписи и визуальные атрибуты для перечислений контракта.
 * Различает состояния формой (иконка + текст), а не только цветом
 * (.ai/frontend-prompt.md «Формулировки»): цвет теряется при печати и на
 * проекторе. Текст самих объяснений (`notes`/`message`/`explanation`)
 * приходит из ответа API — здесь только подписи для фиксированных кодов
 * контракта, не собственные интерпретации.
 */
import BlockIcon from '@mui/icons-material/Block'
import BuildCircleIcon from '@mui/icons-material/BuildCircle'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import ErrorIcon from '@mui/icons-material/Error'
import HelpIcon from '@mui/icons-material/Help'
import HistoryToggleOffIcon from '@mui/icons-material/HistoryToggleOff'
import InfoIcon from '@mui/icons-material/Info'
import ReportProblemIcon from '@mui/icons-material/ReportProblem'
import SatelliteAltIcon from '@mui/icons-material/SatelliteAlt'
import ScheduleIcon from '@mui/icons-material/Schedule'
import SensorsIcon from '@mui/icons-material/Sensors'
import TrendingUpIcon from '@mui/icons-material/TrendingUp'
import type { SvgIconComponent } from '@mui/icons-material'
import type {
  JobStatus,
  LightingStatus,
  Mechanism,
  MechanismStatus,
  RecommendationStatus,
  RecordKind,
  SpaceWeatherEventState,
  WarningSeverity,
} from '../api/types'

export type ChipTone = 'default' | 'success' | 'warning' | 'error' | 'info'

interface LabelSpec {
  label: string
  tone: ChipTone
  icon: SvgIconComponent
}

export const MECHANISM_LABELS: Record<Mechanism, string> = {
  space_weather: 'Космическая погода (поток протонов)',
  mmod: 'MMOD (метеороидная составляющая)',
}

export const MECHANISM_STATUS_LABELS: Record<MechanismStatus, LabelSpec> = {
  ok: { label: 'оценка выполнена', tone: 'success', icon: CheckCircleIcon },
  not_implemented: { label: 'механизм ещё не реализован', tone: 'default', icon: BuildCircleIcon },
  missing_data: { label: 'данных нет', tone: 'warning', icon: HelpIcon },
  stale_data: { label: 'данные устарели', tone: 'warning', icon: HistoryToggleOffIcon },
  source_error: { label: 'отказ источника', tone: 'error', icon: ErrorIcon },
  beyond_horizon: { label: 'за горизонтом прогноза', tone: 'info', icon: ScheduleIcon },
  qualitative_only: {
    label: 'событие подтверждено, уровень не восстановим',
    tone: 'error',
    icon: ReportProblemIcon,
  },
}

/**
 * FN-41: три состояния архивной событийной линии Механизма 1. Подписи
 * намеренно НЕ сокращают «покрытие не подтверждено» до «нет данных» и не
 * превращают «событие не выявлено» в «спокойно»: это разные утверждения о
 * разной степени знания (main-prompt.md §2). NOT_APPLICABLE показывается
 * нейтрально — линия не применялась, и это не вывод об обстановке.
 */
export const EVENT_STATE_LABELS: Record<SpaceWeatherEventState, LabelSpec> = {
  EVENT_PRESENT: { label: 'событие выявлено', tone: 'error', icon: ReportProblemIcon },
  NO_EVENT_DETECTED: {
    label: 'событие не выявлено (покрытие подтверждено)',
    tone: 'success',
    icon: CheckCircleIcon,
  },
  INSUFFICIENT_DATA: {
    label: 'оценить невозможно: покрытие не подтверждено',
    tone: 'warning',
    icon: HelpIcon,
  },
  NOT_APPLICABLE: {
    label: 'архивная событийная линия не применялась',
    tone: 'default',
    icon: BlockIcon,
  },
}

export const WARNING_SEVERITY_LABELS: Record<WarningSeverity, LabelSpec> = {
  info: { label: 'информация', tone: 'default', icon: InfoIcon },
  advisory: { label: 'предупреждение', tone: 'warning', icon: ReportProblemIcon },
  critical: { label: 'критично', tone: 'error', icon: ErrorIcon },
}

export const LIGHTING_STATUS_LABELS: Record<LightingStatus, LabelSpec> = {
  not_requested: { label: 'освещённость не задавалась', tone: 'default', icon: InfoIcon },
  satisfied: { label: 'условие освещённости выполнено', tone: 'success', icon: CheckCircleIcon },
  violated: { label: 'условие освещённости нарушено', tone: 'warning', icon: ReportProblemIcon },
}

export const RECOMMENDATION_STATUS_LABELS: Record<RecommendationStatus, string> = {
  selected: 'Окно выбрано',
  tie: 'Окна равнозначны',
  insufficient_basis: 'Оснований для рекомендации недостаточно',
  all_windows_excluded: 'Все окна исключены из сравнения',
}

export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  pending: 'в очереди',
  running: 'выполняется',
  done: 'готово',
  failed: 'ошибка',
}

export const RECORD_KIND_LABELS: Record<RecordKind, { label: string; icon: SvgIconComponent }> = {
  observation: { label: 'наблюдение', icon: SensorsIcon },
  forecast: { label: 'внешний прогноз', icon: TrendingUpIcon },
  warning: { label: 'предупреждение источника', icon: ReportProblemIcon },
  orbital_elements: { label: 'орбитальные элементы', icon: SatelliteAltIcon },
}

export const EXCLUDED_WINDOW_ICON = BlockIcon
