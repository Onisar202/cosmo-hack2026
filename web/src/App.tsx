import { useState } from 'react'
import AppBar from '@mui/material/AppBar'
import Box from '@mui/material/Box'
import Container from '@mui/material/Container'
import Grid from '@mui/material/Grid'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'
import { CalculationPanel } from './components/CalculationPanel'
import { DemoFixturesPanel } from './components/DemoFixturesPanel'
import { SavedResultsPanel } from './components/SavedResultsPanel'
import { SourceStatusPanel } from './components/SourceStatusPanel'

type TabKey = 'calculate' | 'saved' | 'demo'

function App() {
  const [tab, setTab] = useState<TabKey>('calculate')

  return (
    <>
      <AppBar position="static" color="primary" enableColorOnDark>
        <Toolbar>
          <Typography variant="h6" component="h1">
            Прогнозирование внешних рисков ВКД — прототип
          </Typography>
        </Toolbar>
      </AppBar>

      <Container maxWidth="lg" sx={{ py: 3 }}>
        <Tabs value={tab} onChange={(_, value: TabKey) => setTab(value)} sx={{ mb: 3 }}>
          <Tab value="calculate" label="Расчёт" />
          <Tab value="saved" label="Сохранённые результаты" />
          <Tab value="demo" label="Демо-фикстуры" />
        </Tabs>

        {tab === 'calculate' && (
          <Grid container spacing={3}>
            <Grid size={{ xs: 12, md: 8 }}>
              <CalculationPanel />
            </Grid>
            <Grid size={{ xs: 12, md: 4 }}>
              <Box sx={{ position: { md: 'sticky' }, top: { md: 16 } }}>
                <SourceStatusPanel />
              </Box>
            </Grid>
          </Grid>
        )}

        {tab === 'saved' && <SavedResultsPanel />}
        {tab === 'demo' && <DemoFixturesPanel />}
      </Container>
    </>
  )
}

export default App
