import Accordion from '@mui/material/Accordion'
import AccordionDetails from '@mui/material/AccordionDetails'
import AccordionSummary from '@mui/material/AccordionSummary'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Divider from '@mui/material/Divider'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { CalculationResult } from '../api/types'
import { DataManifestTable } from './DataManifestTable'
import { OrbitSummaryCard } from './OrbitSummaryCard'
import { RecommendationCard } from './RecommendationCard'
import { SourceStatusList } from './SourceStatusList'
import { TimeValue } from './TimeValue'
import { WarningsList } from './WarningsList'
import { WindowCard } from './WindowCard'

const MODE_LABELS: Record<CalculationResult['mode'], string> = {
  current: 'Текущий расчёт',
  historical_analysis: 'Исторический разбор',
  historical_forecast: 'Строгий прогноз из прошлого',
}

/**
 * Единственная точка отображения сохранённого результата — используется и
 * для «живого» расчёта, и для просмотра сохранённого запроса, и для панели
 * демо-фикстур (.ai/frontend-prompt.md: «интерфейс и оба формата выгрузки
 * читают один и тот же сохранённый объект»).
 */
export function ResultView({
  result,
  demo = false,
  stale = false,
}: {
  result: CalculationResult
  demo?: boolean
  stale?: boolean
}) {
  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <Typography variant="h6">Результат {result.result_id}</Typography>
        <Chip size="small" label={MODE_LABELS[result.mode]} />
        <Chip size="small" variant="outlined" label={`алгоритм v${result.algorithm_version}`} />
        {demo && <Chip size="small" color="secondary" label="ДЕМО (фикстура)" />}
        {stale && !demo && (
          <Chip size="small" color="warning" label="относится к прежним параметрам формы" />
        )}
      </Stack>

      <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap' }}>
        <TimeValue label="Рассчитано" iso={result.computed_at} showAgo />
        <TimeValue
          label="Отсечение as_of"
          iso={result.as_of}
          nullReason="не применяется для этого режима"
        />
      </Stack>

      {stale && !demo && (
        <Alert severity="warning">
          Форма изменена после этого расчёта — показанный результат по-прежнему относится к прежним
          параметрам запроса (см. блок «Запрос» ниже). Нажмите «Рассчитать», чтобы обновить.
        </Alert>
      )}

      <OrbitSummaryCard orbit={result.orbit} />

      <RecommendationCard recommendation={result.recommendation} />

      <Box>
        <Typography variant="h6" sx={{ mb: 1 }}>
          Окна
        </Typography>
        <Stack spacing={2}>
          {result.windows.map((win) => (
            <WindowCard
              key={win.window_id}
              window={win}
              isSelected={result.recommendation.window_id === win.window_id}
            />
          ))}
        </Stack>
      </Box>

      <Box>
        <Typography variant="h6" sx={{ mb: 1 }}>
          Предупреждения
        </Typography>
        <WarningsList warnings={result.warnings} />
      </Box>

      {result.limitations.length > 0 && (
        <Alert severity="info">
          <Typography variant="subtitle2">Ограничения применимости</Typography>
          <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
            {result.limitations.map((limitation) => (
              <Typography key={limitation} component="li" variant="body2">
                {limitation}
              </Typography>
            ))}
          </Box>
        </Alert>
      )}

      {result.coverage.archive_gaps.length > 0 && (
        <Alert severity="info">
          <Typography variant="subtitle2">Пробелы архива</Typography>
          <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
            {result.coverage.archive_gaps.map((gap) => (
              <Typography key={`${gap.source_id}-${gap.gap_start}`} component="li" variant="body2">
                {gap.source_id}: {gap.gap_start} — {gap.gap_end}. {gap.note}
              </Typography>
            ))}
          </Box>
        </Alert>
      )}

      <Divider />

      <Accordion>
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Typography variant="subtitle1">Доказательства: манифест данных</Typography>
        </AccordionSummary>
        <AccordionDetails>
          <DataManifestTable entries={result.data_manifest} />
        </AccordionDetails>
      </Accordion>

      <Accordion>
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Typography variant="subtitle1">Статусы источников в этом расчёте</Typography>
        </AccordionSummary>
        <AccordionDetails>
          <SourceStatusList items={result.source_status} />
        </AccordionDetails>
      </Accordion>

      <Accordion>
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Typography variant="subtitle1">Запрос, породивший этот результат</Typography>
        </AccordionSummary>
        <AccordionDetails>
          <Stack spacing={0.5}>
            <Typography variant="body2">
              <strong>Режим:</strong> {MODE_LABELS[result.request.mode]}
            </Typography>
            <TimeValue label="Начало ВКД" iso={result.request.start_at} />
            <Typography variant="body2">
              <strong>Длительность:</strong> {result.request.duration_hours} ч
            </Typography>
            <Typography variant="body2">
              <strong>Период поиска:</strong> {result.request.search_window_hours} ч
            </Typography>
            <TimeValue
              label="Отсечение as_of"
              iso={result.request.as_of ?? null}
              nullReason="не применяется для этого режима"
            />
          </Stack>
        </AccordionDetails>
      </Accordion>
    </Stack>
  )
}
