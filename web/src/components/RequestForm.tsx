import { useState, type FormEvent } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import FormControl from '@mui/material/FormControl'
import FormHelperText from '@mui/material/FormHelperText'
import InputLabel from '@mui/material/InputLabel'
import MenuItem from '@mui/material/MenuItem'
import Select, { type SelectChangeEvent } from '@mui/material/Select'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import type { CalculationRequest, Mode } from '../api/types'
import {
  addHoursIso,
  diffHours,
  isoFromUtcInputValue,
  nowIso,
  utcInputValueFromIso,
} from '../utils/time'

// Единственный источник истины для границ обязательного исторического
// периода — contracts/README.md, раздел «Обязательный исторический
// период». Дублируется здесь только для подсказки в форме до отправки
// запроса — окончательную проверку всегда делает сервис.
const ARCHIVE_START = '2024-05-01T00:00:00Z'
const ARCHIVE_END = '2024-06-30T23:59:59Z'

const MODE_LABELS: Record<Mode, string> = {
  current: 'Текущий расчёт',
  historical_analysis: 'Исторический разбор (полный архив, без отсечения)',
  historical_forecast: 'Строгий прогноз из прошлого (с отсечением as_of)',
}

interface FormValues {
  mode: Mode
  startAt: string
  durationHours: string
  searchWindowHours: string
  asOf: string
}

function defaultValues(): FormValues {
  const start = addHoursIso(nowIso(), 1)
  return {
    mode: 'current',
    startAt: utcInputValueFromIso(start),
    durationHours: '4',
    searchWindowHours: '8',
    asOf: '',
  }
}

export function RequestForm({
  onSubmit,
  onDirty,
  disabled = false,
}: {
  onSubmit: (payload: CalculationRequest) => void
  onDirty: () => void
  disabled?: boolean
}) {
  const [values, setValues] = useState<FormValues>(defaultValues)
  const [error, setError] = useState<string | null>(null)

  function update<K extends keyof FormValues>(key: K, value: FormValues[K]) {
    setValues((prev) => ({ ...prev, [key]: value }))
    onDirty()
  }

  function handleModeChange(event: SelectChangeEvent<Mode>) {
    const mode = event.target.value as Mode
    onDirty()
    setValues((prev) => ({ ...prev, mode, asOf: mode === 'historical_forecast' ? prev.asOf : '' }))
  }

  function validate(): CalculationRequest | null {
    const startAtIso = isoFromUtcInputValue(values.startAt)
    if (!startAtIso) {
      setError('Начало ВКД (start_at) указано некорректно.')
      return null
    }

    const durationHours = Number(values.durationHours)
    if (!Number.isFinite(durationHours) || durationHours < 1 || durationHours > 8) {
      setError('Длительность ВКД — число от 1 до 8 часов.')
      return null
    }

    const searchWindowHours = Number(values.searchWindowHours)
    if (!Number.isFinite(searchWindowHours) || searchWindowHours < 0 || searchWindowHours > 24) {
      setError('Период поиска начала — число от 0 до 24 часов.')
      return null
    }

    let asOfIso: string | undefined
    if (values.mode === 'historical_forecast') {
      asOfIso = isoFromUtcInputValue(values.asOf) ?? undefined
      if (!asOfIso) {
        setError('Момент отсечения (as_of) обязателен для строгого прогноза из прошлого.')
        return null
      }
      if (diffHours(startAtIso, asOfIso) > 0) {
        setError(
          'Момент отсечения (as_of) должен быть не позже начала ВКД — иначе прогноз из прошлого ' +
            'превращается в разбор по факту (contracts/README.md).',
        )
        return null
      }
    }

    if (values.mode !== 'current') {
      if (startAtIso < ARCHIVE_START || startAtIso > ARCHIVE_END) {
        setError(
          `Для исторических режимов начало ВКД должно попадать в поддерживаемый период ` +
            `${ARCHIVE_START} — ${ARCHIVE_END}.`,
        )
        return null
      }
    }

    setError(null)
    return {
      mode: values.mode,
      start_at: startAtIso,
      duration_hours: durationHours,
      search_window_hours: searchWindowHours,
      ...(asOfIso ? { as_of: asOfIso } : {}),
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const payload = validate()
    if (payload) {
      onSubmit(payload)
    }
  }

  return (
    <Box component="form" onSubmit={handleSubmit} noValidate>
      <Stack spacing={2}>
        <Typography variant="h6">Параметры ВКД</Typography>

        <FormControl fullWidth size="small">
          <InputLabel id="mode-label">Режим</InputLabel>
          <Select
            labelId="mode-label"
            label="Режим"
            value={values.mode}
            onChange={handleModeChange}
          >
            {(Object.keys(MODE_LABELS) as Mode[]).map((mode) => (
              <MenuItem key={mode} value={mode}>
                {MODE_LABELS[mode]}
              </MenuItem>
            ))}
          </Select>
          <FormHelperText>
            Три режима не смешиваются: режим — часть запроса и результата.
          </FormHelperText>
        </FormControl>

        <TextField
          label="Начало ВКД (UTC)"
          type="datetime-local"
          size="small"
          value={values.startAt}
          onChange={(e) => update('startAt', e.target.value)}
          slotProps={{ inputLabel: { shrink: true } }}
          helperText="Поле трактуется как время в UTC — без подстановки часового пояса браузера."
          fullWidth
        />

        <Stack direction="row" spacing={2}>
          <TextField
            label="Длительность ВКД, ч"
            type="number"
            size="small"
            value={values.durationHours}
            onChange={(e) => update('durationHours', e.target.value)}
            slotProps={{ htmlInput: { min: 1, max: 8, step: 0.5 } }}
            fullWidth
          />
          <TextField
            label="Период поиска начала, ч"
            type="number"
            size="small"
            value={values.searchWindowHours}
            onChange={(e) => update('searchWindowHours', e.target.value)}
            slotProps={{ htmlInput: { min: 0, max: 24, step: 0.5 } }}
            fullWidth
          />
        </Stack>

        {values.mode === 'historical_forecast' && (
          <TextField
            label="Отсечение as_of (UTC)"
            type="datetime-local"
            size="small"
            value={values.asOf}
            onChange={(e) => update('asOf', e.target.value)}
            slotProps={{ inputLabel: { shrink: true } }}
            helperText="Во вход расчёта попадают только записи, опубликованные до этого момента."
            fullWidth
          />
        )}

        {error && <Alert severity="error">{error}</Alert>}

        <Button type="submit" variant="contained" disabled={disabled}>
          Рассчитать
        </Button>
      </Stack>
    </Box>
  )
}
