import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { CalculationWarning } from '../api/types'
import { WARNING_SEVERITY_LABELS } from './labels'
import { StatusBadge } from './StatusBadge'

/**
 * Каждое предупреждение доказуемо: ссылается на `record_ids` и/или
 * `fetch_attempt_id` (contracts/README.md «Прослеживаемость») — оба
 * показаны как есть, без придумывания причины, если сервис их не прислал.
 */
export function WarningsList({ warnings }: { warnings: CalculationWarning[] }) {
  if (warnings.length === 0) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Предупреждений нет.
      </Typography>
    )
  }

  return (
    <Stack spacing={1.5}>
      {warnings.map((warning, index) => {
        const severity = WARNING_SEVERITY_LABELS[warning.severity]
        return (
          <Stack
            key={`${warning.code}-${index}`}
            spacing={0.5}
            sx={{ border: 1, borderColor: 'divider', borderRadius: 1, p: 1.5 }}
          >
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
              <StatusBadge label={severity.label} tone={severity.tone} icon={severity.icon} />
              <Typography variant="body2" sx={{ fontFamily: 'monospace', color: 'text.secondary' }}>
                {warning.code}
              </Typography>
              {warning.window_id && (
                <Chip size="small" variant="outlined" label={`окно ${warning.window_id}`} />
              )}
              {warning.mechanism && (
                <Chip size="small" variant="outlined" label={warning.mechanism} />
              )}
            </Stack>
            <Typography variant="body2">{warning.message}</Typography>
            <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap' }}>
              {warning.record_ids.map((id) => (
                <Chip
                  key={id}
                  size="small"
                  label={id}
                  variant="outlined"
                  sx={{ fontFamily: 'monospace' }}
                />
              ))}
              {warning.fetch_attempt_id && (
                <Chip
                  size="small"
                  label={`попытка: ${warning.fetch_attempt_id}`}
                  variant="outlined"
                  sx={{ fontFamily: 'monospace' }}
                />
              )}
            </Stack>
          </Stack>
        )
      })}
    </Stack>
  )
}
