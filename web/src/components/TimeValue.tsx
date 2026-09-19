import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { formatAgo, formatUtc } from '../utils/time'
import { NullValue } from './NullValue'

/**
 * Единственный способ показать момент времени в интерфейсе. Каждое время
 * подписано, КАКОЕ оно (измерение/интервал действия/публикация/получение/
 * отсечение) — .ai/frontend-prompt.md «12:40 UTC без подписи бесполезна: на
 * одном экране их до пяти». Никогда не форматирует через toLocaleString()
 * или Date.toString() — вся арифметика в src/utils/time.ts.
 */
export function TimeValue({
  label,
  iso,
  showAgo = false,
  nullReason = 'время неизвестно',
}: {
  label: string
  iso: string | null
  showAgo?: boolean
  nullReason?: string
}) {
  return (
    <Box component="span" sx={{ display: 'inline-flex', gap: 0.5, alignItems: 'baseline' }}>
      <Typography component="span" variant="body2" sx={{ color: 'text.secondary' }}>
        {label}:
      </Typography>
      {iso == null ? (
        <NullValue reason={nullReason} />
      ) : (
        <Typography component="span" variant="body2">
          {formatUtc(iso)}
          {showAgo ? ` · ${formatAgo(iso)}` : ''}
        </Typography>
      )}
    </Box>
  )
}
