import { useCallback, useEffect, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import ListItemText from '@mui/material/ListItemText'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { getResult, listResults } from '../api/client'
import type { CalculationResult, ResultListItem } from '../api/types'
import { formatUtc } from '../utils/time'
import { ResultView } from './ResultView'

/**
 * Просмотр сохранённого запроса: по идентификатору или из списка последних
 * расчётов (`GET /api/results`, `GET /api/results/{id}`). Результат
 * рендерится тем же ResultView, что и живой расчёт и демо-фикстуры.
 */
export function SavedResultsPanel() {
  const [list, setList] = useState<ResultListItem[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [manualId, setManualId] = useState('')
  const [selected, setSelected] = useState<CalculationResult | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const seqRef = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    listResults({ limit: 20 }, controller.signal)
      .then(setList)
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setListError(err instanceof Error ? err.message : 'Не удалось получить список результатов.')
      })
    return () => controller.abort()
  }, [])

  const open = useCallback((resultId: string) => {
    const mySeq = ++seqRef.current
    const controller = new AbortController()
    setLoading(true)
    setLoadError(null)
    getResult(resultId, controller.signal)
      .then((result) => {
        if (seqRef.current !== mySeq) return
        setSelected(result)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || seqRef.current !== mySeq) return
        setLoadError(
          err instanceof Error ? err.message : `Не удалось загрузить результат ${resultId}.`,
        )
      })
      .finally(() => {
        if (seqRef.current === mySeq) setLoading(false)
      })
  }, [])

  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={1}>
        <TextField
          label="result_id"
          size="small"
          value={manualId}
          onChange={(e) => setManualId(e.target.value)}
          fullWidth
        />
        <Button variant="outlined" disabled={!manualId || loading} onClick={() => open(manualId)}>
          Открыть
        </Button>
      </Stack>

      {listError && <Alert severity="error">{listError}</Alert>}

      {list && list.length > 0 && (
        <Box>
          <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
            Последние сохранённые расчёты
          </Typography>
          <List dense sx={{ border: 1, borderColor: 'divider', borderRadius: 1 }}>
            {list.map((item) => (
              <ListItemButton key={item.result_id} onClick={() => open(item.result_id)}>
                <ListItemText
                  primary={`${item.result_id} — ${item.mode}`}
                  secondary={`${formatUtc(item.computed_at)} · рекомендация: ${item.recommendation_status}`}
                />
              </ListItemButton>
            ))}
          </List>
        </Box>
      )}

      {loading && (
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <CircularProgress size={16} />
          <Typography variant="body2">Загрузка результата…</Typography>
        </Stack>
      )}

      {loadError && <Alert severity="error">{loadError}</Alert>}

      {selected && <ResultView result={selected} />}
    </Stack>
  )
}
