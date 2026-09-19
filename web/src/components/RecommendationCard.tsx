import Alert from '@mui/material/Alert'
import Typography from '@mui/material/Typography'
import type { Recommendation } from '../api/types'
import { RECOMMENDATION_STATUS_LABELS } from './labels'

const SEVERITY_BY_STATUS: Record<Recommendation['status'], 'success' | 'info' | 'warning'> = {
  selected: 'success',
  tie: 'info',
  insufficient_basis: 'warning',
  all_windows_excluded: 'warning',
}

/**
 * Текст рекомендации приходит из ответа API как есть (.ai/frontend-prompt.md
 * «Текст объяснений приходит из расчёта») — интерфейс не добавляет своих
 * формулировок вроде «безопасно»/«риска нет», только заголовок статуса.
 */
export function RecommendationCard({ recommendation }: { recommendation: Recommendation }) {
  return (
    <Alert severity={SEVERITY_BY_STATUS[recommendation.status]} variant="outlined">
      <Typography variant="subtitle2">
        {RECOMMENDATION_STATUS_LABELS[recommendation.status]}
      </Typography>
      <Typography variant="body2">{recommendation.explanation}</Typography>
    </Alert>
  )
}
