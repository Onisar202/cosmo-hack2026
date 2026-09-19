import Chip from '@mui/material/Chip'
import type { SvgIconComponent } from '@mui/icons-material'
import type { ChipTone } from './labels'

const TONE_TO_MUI_COLOR: Record<ChipTone, 'default' | 'success' | 'warning' | 'error' | 'info'> = {
  default: 'default',
  success: 'success',
  warning: 'warning',
  error: 'error',
  info: 'info',
}

/** Значок статуса: иконка + текст + цвет — различимо и без цвета (печать,
 * проектор), см. src/components/labels.ts. */
export function StatusBadge({
  label,
  tone,
  icon: Icon,
}: {
  label: string
  tone: ChipTone
  icon: SvgIconComponent
}) {
  return (
    <Chip
      size="small"
      color={TONE_TO_MUI_COLOR[tone]}
      variant={tone === 'default' ? 'outlined' : 'filled'}
      icon={<Icon fontSize="small" />}
      label={label}
    />
  )
}
