import { useState } from 'react'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import CardActionArea from '@mui/material/CardActionArea'
import CardContent from '@mui/material/CardContent'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { DEMO_FIXTURES } from '../api/fixtures'
import { ResultView } from './ResultView'

/**
 * Демонстрационная панель контракта: каждая из четырёх фикстур
 * (`contracts/fixtures/`) должна отображаться корректно
 * (.ai/frontend-prompt.md «каждый экран проверяется на всех четырёх
 * фикстурах»). Метка «ДЕМО» держится только здесь — не на живом расчёте.
 */
export function DemoFixturesPanel() {
  const [selectedId, setSelectedId] = useState(DEMO_FIXTURES[0].id)
  const selected = DEMO_FIXTURES.find((fixture) => fixture.id === selectedId) ?? DEMO_FIXTURES[0]

  return (
    <Stack spacing={2}>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Четыре фикстуры контракта — форма ответа зафиксирована в `contracts/fixtures/`. Все данные
        демонстрационные и не являются результатом научного расчёта.
      </Typography>

      <Box
        sx={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
          gap: 1.5,
        }}
      >
        {DEMO_FIXTURES.map((fixture) => (
          <Card
            key={fixture.id}
            variant="outlined"
            sx={{ borderColor: fixture.id === selectedId ? 'primary.main' : undefined }}
          >
            <CardActionArea onClick={() => setSelectedId(fixture.id)}>
              <CardContent>
                <Typography variant="subtitle2">{fixture.label}</Typography>
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  {fixture.description}
                </Typography>
              </CardContent>
            </CardActionArea>
          </Card>
        ))}
      </Box>

      <ResultView result={selected.result} demo />
    </Stack>
  )
}
