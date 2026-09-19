import { defineConfig, devices } from '@playwright/test'

/**
 * FN-35 round 1 ревью: `web/tests/uiStates.spec.ts` мокает весь HTTP-контракт
 * через `page.route` и не требует ни живого бэкенда, ни реальной сети — в
 * отличие от `web/tests/stage1.spec.ts` (`playwright.config.ts`, требует
 * `STAGE1_WEB_BASE_URL` уже запущенного стека и поэтому не входит в CI).
 * Эта конфигурация запускает собранный `npm run build` (см. `webServer`
 * ниже) сама и годится для CI (`.github/workflows/ci.yml` → `frontend-checks`
 * → `npm run test:e2e:mock`) — без этого файла заявленное в PR покрытие не
 * защищало бы от регрессий (round 1 ревью).
 */
export default defineConfig({
  testDir: './tests',
  testMatch: /uiStates\.spec\.ts$/,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
  },
  webServer: {
    // `npx vite preview` напрямую, а не `npm run preview --` — на раннере CI
    // (round 1 ревью) обёртка npm добавляла задержку старта. Явный
    // `--host 127.0.0.1` обязателен: без него `vite preview` слушает только
    // хостнейм `localhost`, который на некоторых раннерах CI резолвится в
    // `::1` (IPv6) — Playwright же опрашивает готовность строго по
    // `http://127.0.0.1:4173` (см. `use.baseURL` ниже) и подключиться не
    // может, из-за чего сервер выглядит недоступным все 60 с таймаута, хотя
    // процесс уже поднят (frontend-checks run 35434397746).
    command: 'npx vite preview --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
          : {},
      },
    },
  ],
})
