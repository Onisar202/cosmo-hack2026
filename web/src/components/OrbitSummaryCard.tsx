import SatelliteAltIcon from '@mui/icons-material/SatelliteAlt'
import Alert from '@mui/material/Alert'
import Card from '@mui/material/Card'
import CardContent from '@mui/material/CardContent'
import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { OrbitSummary } from '../api/types'
import { TimeValue } from './TimeValue'

const SOURCE_LABELS: Record<OrbitSummary['source'], string> = {
  celestrak: 'CelesTrak (текущие элементы)',
  'space-track': 'Space-Track GP_HISTORY (исторические элементы)',
  'nasa-iss-oem': 'NASA TOPO CCSDS OEM (исторические эфемериды, интерполяция)',
}

/**
 * Орбитальная сводка: траектория участвует хотя бы в одном расчёте, и
 * видны источник, эпоха и давность элементов (.ai/main-prompt.md §12).
 * Текущие элементы никогда не подставляются вместо исторических —
 * `is_reconstructed` предупреждает об этом честно, а не тихо.
 */
export function OrbitSummaryCard({ orbit }: { orbit: OrbitSummary }) {
  return (
    <Card variant="outlined">
      <CardContent>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 1 }}>
          <SatelliteAltIcon fontSize="small" />
          <Typography variant="subtitle1">Орбита МКС (NORAD {orbit.norad_id})</Typography>
        </Stack>
        <Stack spacing={0.5}>
          <Typography variant="body2">
            <strong>Источник:</strong> {SOURCE_LABELS[orbit.source]}
          </Typography>
          <TimeValue label="Эпоха элементов" iso={orbit.elements_epoch} />
          <Typography variant="body2">
            <strong>Давность элементов:</strong> {orbit.elements_age_hours.toFixed(2)} ч
          </Typography>
          <Typography variant="body2">
            <strong>Система координат:</strong> {orbit.coordinate_system}
          </Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            record_id: {orbit.record_id}
          </Typography>
        </Stack>
        {orbit.is_reconstructed && (
          <Alert severity="warning" sx={{ mt: 1 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              <Chip size="small" color="warning" label="реконструкция" />
              <Typography variant="body2">
                Элементы на момент расчёта не подтверждены — геометрия помечена как реконструкция, а
                не как проверенный прогноз.
              </Typography>
            </Stack>
          </Alert>
        )}
      </CardContent>
    </Card>
  )
}
