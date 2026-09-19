import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'

/**
 * `value: number | string | null` без причины отсутствия на экране
 * превращается в прочерк, который читается как «ничего не происходит»
 * (.ai/frontend-prompt.md «Состояния данных»). Каждое место, где контракт
 * допускает `null`, обязано передать сюда, почему значения нет — а не молча
 * оставить пустую ячейку и не подставить 0.
 */
export function NullValue({ reason }: { reason: string }) {
  return (
    <Tooltip title={reason} arrow>
      <Typography
        component="span"
        variant="inherit"
        sx={{
          color: 'text.disabled',
          fontStyle: 'italic',
          borderBottom: '1px dotted currentColor',
          cursor: 'help',
          whiteSpace: 'nowrap',
        }}
      >
        нет данных
      </Typography>
    </Tooltip>
  )
}
