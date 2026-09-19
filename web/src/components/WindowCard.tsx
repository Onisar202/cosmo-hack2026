import Alert from '@mui/material/Alert'
import Card from '@mui/material/Card'
import CardContent from '@mui/material/CardContent'
import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { CalcWindow } from '../api/types'
import { LIGHTING_STATUS_LABELS } from './labels'
import { MechanismAssessmentCard } from './MechanismAssessmentCard'
import { StatusBadge } from './StatusBadge'
import { TimeValue } from './TimeValue'

/**
 * Окно исключённое из сравнения по критическому пробелу выглядит НЕ как
 * проигравшее — нейтральная рамка и явная пометка причины, не красный
 * крест «отказ» (.ai/frontend-prompt.md «Состояния данных»: «Окно выведено
 * из сравнения по неполноте данных» не должно читаться как «проигравшее»).
 */
export function WindowCard({
  window: win,
  isSelected,
}: {
  window: CalcWindow
  isSelected: boolean
}) {
  const lighting = LIGHTING_STATUS_LABELS[win.lighting.status]

  return (
    <Card
      variant="outlined"
      sx={{
        borderColor: isSelected
          ? 'success.main'
          : win.excluded_from_comparison
            ? 'divider'
            : undefined,
        borderWidth: isSelected ? 2 : 1,
        opacity: win.excluded_from_comparison ? 0.85 : 1,
      }}
    >
      <CardContent>
        <Stack
          direction="row"
          sx={{ justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 1 }}
        >
          <Typography variant="subtitle1">Окно {win.window_id}</Typography>
          <Stack direction="row" spacing={1}>
            {isSelected && <Chip size="small" color="success" label="рекомендовано" />}
            {win.excluded_from_comparison && (
              <Chip size="small" variant="outlined" label="исключено из сравнения" />
            )}
          </Stack>
        </Stack>

        <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', my: 1 }}>
          <TimeValue label="Начало" iso={win.start_at} />
          <TimeValue label="Окончание" iso={win.end_at} />
          <Typography variant="body2">
            <strong>Длительность:</strong> {win.duration_hours} ч
          </Typography>
        </Stack>

        {win.excluded_from_comparison && win.exclusion_reason && (
          <Alert severity="info" sx={{ mb: 1.5 }}>
            {win.exclusion_reason}
          </Alert>
        )}

        <Stack spacing={1.5}>
          {win.mechanisms.map((assessment) => (
            <MechanismAssessmentCard key={assessment.mechanism} assessment={assessment} />
          ))}
        </Stack>

        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mt: 1.5 }}>
          <StatusBadge label={lighting.label} tone={lighting.tone} icon={lighting.icon} />
          {win.lighting.note && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              {win.lighting.note}
            </Typography>
          )}
        </Stack>
      </CardContent>
    </Card>
  )
}
