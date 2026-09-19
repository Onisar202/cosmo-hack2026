import { useCallback, useEffect, useRef, useState } from 'react'
import RefreshIcon from '@mui/icons-material/Refresh'
import Alert from '@mui/material/Alert'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { getSourcesStatus, refreshSources } from '../api/client'
import type { SourceStatus } from '../api/types'
import { SourceStatusList } from './SourceStatusList'

/**
 * Живой статус источников (`GET /api/sources/status`) с принудительным
 * обновлением (`POST /api/sources/refresh`) — «видно время последнего
 * успешного получения; есть принудительное обновление» (.ai/main-prompt.md
 * §12). Ответы устаревших запросов (быстрый повторный клик) отбрасываются
 * по тому же принципу, что и в src/api/useCalculation.ts.
 */
export function SourceStatusPanel() {
  const [items, setItems] = useState<SourceStatus[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // true изначально: первая загрузка запускается эффектом при монтировании.
  const [loading, setLoading] = useState(true)
  const seqRef = useRef(0)

  const fetchStatuses = useCallback((fetcher: (signal: AbortSignal) => Promise<SourceStatus[]>) => {
    const mySeq = ++seqRef.current
    const controller = new AbortController()
    fetcher(controller.signal)
      .then((result) => {
        if (seqRef.current !== mySeq) return
        setItems(result)
        setError(null)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || seqRef.current !== mySeq) return
        setError(err instanceof Error ? err.message : 'Не удалось получить статусы источников.')
      })
      .finally(() => {
        if (seqRef.current === mySeq) setLoading(false)
      })
    return () => controller.abort()
  }, [])

  useEffect(() => fetchStatuses((signal) => getSourcesStatus(signal)), [fetchStatuses])

  function handleRefreshClick() {
    setLoading(true)
    fetchStatuses((signal) => refreshSources(signal))
  }

  return (
    <Stack spacing={1}>
      <Stack direction="row" sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
        <Typography variant="h6">Источники данных</Typography>
        <Button
          size="small"
          startIcon={loading ? <CircularProgress size={14} /> : <RefreshIcon />}
          disabled={loading}
          onClick={handleRefreshClick}
        >
          Обновить источники
        </Button>
      </Stack>
      {error && <Alert severity="error">{error}</Alert>}
      {items && <SourceStatusList items={items} />}
    </Stack>
  )
}
