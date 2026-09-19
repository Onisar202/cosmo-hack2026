import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useCalculation } from '../api/useCalculation'
import { JOB_STATUS_LABELS } from './labels'
import { RequestForm } from './RequestForm'
import { ResultView } from './ResultView'

/**
 * Пользовательский путь «задать окно → рассчитать → изучить воздействия»
 * (.ai/frontend-prompt.md). Гонка запросов и устаревание результата под
 * изменённой формой обрабатываются в src/api/useCalculation.ts — здесь
 * только раскладка статуса по экрану.
 */
export function CalculationPanel() {
  const { state, run, markStale } = useCalculation()

  return (
    <Stack spacing={3}>
      <RequestForm
        onSubmit={run}
        onDirty={markStale}
        disabled={state.phase === 'pending' || state.phase === 'running'}
      />

      {(state.phase === 'pending' || state.phase === 'running') && (
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <CircularProgress size={18} />
          <Typography variant="body2">
            Расчёт {JOB_STATUS_LABELS[state.phase]}
            {state.taskId ? ` (task_id: ${state.taskId})` : ''}…
          </Typography>
        </Stack>
      )}

      {state.phase === 'failed' && (
        <Alert severity="error">
          <Typography variant="subtitle2">
            Расчёт завершился ошибкой ({state.error.code})
          </Typography>
          <Typography variant="body2">{state.error.message}</Typography>
          <Typography variant="body2" sx={{ mt: 1, color: 'text.secondary' }}>
            Измените параметры или повторите запрос — задачу можно запустить заново.
          </Typography>
          {state.stale && (
            <Typography variant="body2" sx={{ mt: 1, fontWeight: 600 }}>
              Форма изменена после этой попытки — сообщение об ошибке относится к прежним параметрам
              запроса.
            </Typography>
          )}
        </Alert>
      )}

      {state.phase === 'done' && (
        <Box>
          <ResultView result={state.result} stale={state.stale} />
        </Box>
      )}
    </Stack>
  )
}
