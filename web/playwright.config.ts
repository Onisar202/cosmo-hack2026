import { defineConfig, devices } from '@playwright/test'

/**
 * FN-28 (S1-10): сквозная UI-проверка (web/tests/stage1.spec.ts) против уже
 * запущенного стека — docker compose (`http://localhost:8080`) либо
 * `npm run preview` + `uv run python -m src.api.app` локально без Docker
 * (см. корневой README.md «Развёртывание»).
 *
 * Отдельная команда `npm run test:e2e` — не часть `npm run test` (vitest,
 * компонентные тесты без сети из web/tests/*.test.tsx), потому что требует
 * реально запущенного бэкенда и настоящей сети до источников, а не только
 * jsdom.
 */
export default defineConfig({
  testDir: './tests',
  testMatch: /.*\.spec\.ts$/,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.STAGE1_WEB_BASE_URL ?? 'http://127.0.0.1:8080',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // Некоторые окружения (например CI-песочница) держат Chromium по
        // фиксированному пути и не дают `playwright install` скачать
        // ревизию, зашитую в установленную версию @playwright/test —
        // PLAYWRIGHT_CHROMIUM_PATH указывает на такой уже установленный
        // браузер явно. Без переменной используется браузер, который
        // поставит `playwright install` обычным способом.
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
          : {},
      },
    },
  ],
})
