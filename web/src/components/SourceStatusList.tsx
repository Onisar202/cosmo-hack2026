import Chip from '@mui/material/Chip'
import List from '@mui/material/List'
import ListItem from '@mui/material/ListItem'
import ListItemText from '@mui/material/ListItemText'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { SourceStatus } from '../api/types'
import { TimeValue } from './TimeValue'

/**
 * Статус источника — отдельная сущность, не подмешанная в оценку
 * (.ai/main-prompt.md §5). Отказ, квота и заморозка видны явно, а не
 * скрыты за «последним успешным» значением.
 */
export function SourceStatusList({ items }: { items: SourceStatus[] }) {
  if (items.length === 0) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Статусы источников недоступны.
      </Typography>
    )
  }

  return (
    <List dense disablePadding>
      {items.map((status) => (
        <ListItem key={status.source_id} divider disableGutters sx={{ display: 'block', py: 1 }}>
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 0.5 }}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              {status.source_id}
            </Typography>
            {status.frozen && (
              <Chip size="small" label="заморожен" color="default" variant="outlined" />
            )}
            {status.quota_limited && <Chip size="small" label="квота исчерпана" color="warning" />}
          </Stack>
          <ListItemText
            disableTypography
            sx={{ m: 0 }}
            primary={
              <Stack spacing={0.25}>
                <TimeValue
                  label="Последний успех"
                  iso={status.last_success_at}
                  showAgo
                  nullReason="ни одного успешного обращения"
                />
                <TimeValue
                  label="Последняя ошибка"
                  iso={status.last_error_at}
                  showAgo
                  nullReason="ошибок не было"
                />
                {status.last_error_message && (
                  <Typography variant="body2" sx={{ color: 'error.main' }}>
                    {status.last_error_message}
                  </Typography>
                )}
              </Stack>
            }
          />
        </ListItem>
      ))}
    </List>
  )
}
