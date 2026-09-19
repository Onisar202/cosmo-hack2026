import Stack from '@mui/material/Stack'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import Typography from '@mui/material/Typography'
import type { DataManifestEntry } from '../api/types'
import { RECORD_KIND_LABELS } from './labels'

/**
 * Перечень записей, фактически попавших в расчёт (О4 «от предупреждения —
 * к значению и первоисточнику»). Вид записи (наблюдение/прогноз/
 * предупреждение/орбитальные элементы) различается иконкой и текстом, а не
 * только цветом (.ai/frontend-prompt.md «Формулировки»).
 */
export function DataManifestTable({ entries }: { entries: DataManifestEntry[] }) {
  if (entries.length === 0) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Манифест пуст.
      </Typography>
    )
  }

  return (
    <Table size="small">
      <TableHead>
        <TableRow>
          <TableCell>record_id</TableCell>
          <TableCell>Источник</TableCell>
          <TableCell>Версия источника</TableCell>
          <TableCell>Вид записи</TableCell>
        </TableRow>
      </TableHead>
      <TableBody>
        {entries.map((entry) => {
          const kind = RECORD_KIND_LABELS[entry.record_kind]
          const Icon = kind.icon
          return (
            <TableRow key={entry.record_id}>
              <TableCell sx={{ fontFamily: 'monospace' }}>{entry.record_id}</TableCell>
              <TableCell>{entry.source_id}</TableCell>
              <TableCell sx={{ fontFamily: 'monospace' }}>{entry.source_version}</TableCell>
              <TableCell>
                <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
                  <Icon fontSize="inherit" />
                  <span>{kind.label}</span>
                </Stack>
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}
