import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { MechanismAssessment } from '../api/types'
import {
  EVENT_STATE_LABELS,
  MECHANISM_LABELS,
  MECHANISM_STATUS_LABELS,
  SOURCE_AGREEMENT_LABELS,
} from './labels'
import { NullValue } from './NullValue'
import { StatusBadge } from './StatusBadge'

const REASON_BY_STATUS: Record<string, string> = {
  not_implemented: 'механизм ещё не реализован в этой версии сервиса',
  missing_data: 'источник не вернул данные за интервал окна',
  stale_data: 'доступные данные устарели',
  source_error: 'отказ или квота источника',
  beyond_horizon: 'интервал за пределами горизонта прогноза — «не покрыто», а не «спокойно»',
  qualitative_only:
    'событие подтверждено архивной линией, но уровень и длительность превышения по ней ' +
    'не восстанавливаются — это не «спокойно» и не «нет данных»',
}

/**
 * Оценка одного механизма для одного окна. `max_level`/`exceedance_hours_by_level`
 * — null ровно когда `status != ok`; NullValue несёт причину, а не прочерк
 * без объяснения (.ai/frontend-prompt.md «Состояния данных»).
 */
export function MechanismAssessmentCard({ assessment }: { assessment: MechanismAssessment }) {
  const statusSpec = MECHANISM_STATUS_LABELS[assessment.status]
  const nullReason = REASON_BY_STATUS[assessment.status] ?? 'нет данных для оценки'
  // FN-41: поле обязательно контрактом, но сохранённые результаты прежних
  // версий сервиса его не несут — хранилище неизменяемо и продолжает их
  // отдавать (main-prompt.md §3), поэтому строка показывается только когда
  // состояние действительно есть, а не подменяется значением по умолчанию.
  const eventStateSpec =
    assessment.event_state == null ? null : EVENT_STATE_LABELS[assessment.event_state]
  // Та же оговорка: поле обязательно контрактом с FN-41, но результаты,
  // сохранённые до него, его не несут.
  const agreementSpec =
    assessment.source_agreement == null
      ? null
      : SOURCE_AGREEMENT_LABELS[assessment.source_agreement]
  const sourceLines = assessment.source_assessments ?? []

  return (
    <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 1, p: 1.5 }}>
      <Stack direction="row" sx={{ justifyContent: 'space-between', alignItems: 'center', mb: 1 }}>
        <Typography variant="subtitle2">{MECHANISM_LABELS[assessment.mechanism]}</Typography>
        <StatusBadge label={statusSpec.label} tone={statusSpec.tone} icon={statusSpec.icon} />
      </Stack>

      <Stack spacing={0.5}>
        {eventStateSpec != null && (
          <Typography variant="body2" component="div">
            <strong>Архивная событийная линия:</strong>{' '}
            <StatusBadge
              label={eventStateSpec.label}
              tone={eventStateSpec.tone}
              icon={eventStateSpec.icon}
            />
          </Typography>
        )}
        {agreementSpec != null && (
          <Typography variant="body2" component="div">
            <strong>Согласие источников внутри механизма:</strong>{' '}
            <StatusBadge
              label={agreementSpec.label}
              tone={agreementSpec.tone}
              icon={agreementSpec.icon}
            />
          </Typography>
        )}
        {sourceLines.length > 0 && (
          <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
            {sourceLines.map((line) => (
              <Typography key={line.source_id} component="li" variant="body2">
                <code>{line.source_id}</code>: {EVENT_STATE_LABELS[line.event_state].label},
                покрытие {(line.coverage_fraction * 100).toFixed(0)}%
                {line.beyond_horizon ? ' · за горизонтом линии' : ''}
                {line.record_ids.length > 0 && ` · записи: ${line.record_ids.join(', ')}`}
              </Typography>
            ))}
          </Box>
        )}
        <Typography variant="body2">
          <strong>Максимальный уровень:</strong>{' '}
          {assessment.max_level == null ? <NullValue reason={nullReason} /> : assessment.max_level}
        </Typography>

        <Typography variant="body2">
          <strong>Длительность превышения по порогам:</strong>
        </Typography>
        {assessment.exceedance_hours_by_level == null ? (
          <NullValue reason={nullReason} />
        ) : (
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}>
            {Object.entries(assessment.exceedance_hours_by_level).map(([level, hours]) => (
              <Chip key={level} size="small" label={`${level}: ${hours} ч`} variant="outlined" />
            ))}
          </Stack>
        )}

        <Typography variant="body2">
          <strong>Полнота данных:</strong> {(assessment.coverage_fraction * 100).toFixed(0)}%
        </Typography>

        {assessment.critical_gap && (
          <Chip
            size="small"
            color="error"
            variant="outlined"
            label="критический пробел данных"
            sx={{ width: 'fit-content' }}
          />
        )}

        {assessment.notes.length > 0 && (
          <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
            {assessment.notes.map((note) => (
              <Typography
                key={note}
                component="li"
                variant="body2"
                sx={{ color: 'text.secondary' }}
              >
                {note}
              </Typography>
            ))}
          </Box>
        )}

        {assessment.record_ids.length > 0 && (
          <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap', mt: 0.5 }}>
            {assessment.record_ids.map((id) => (
              <Chip
                key={id}
                size="small"
                label={id}
                variant="outlined"
                sx={{ fontFamily: 'monospace' }}
              />
            ))}
          </Stack>
        )}
      </Stack>
    </Box>
  )
}
