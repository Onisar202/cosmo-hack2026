import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { configDefaults } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Backend (src/api, FastAPI) served separately — see README "Запуск".
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    css: false,
    // *.spec.ts — конвенция Playwright (playwright.config.ts, реальный
    // браузер и запущенный бэкенд), а не vitest/jsdom: без исключения
    // `test.describe` из web/tests/stage1.spec.ts падает под vitest,
    // у которого свой рантайм `test`/`describe` без Playwright-фикстур.
    exclude: [...configDefaults.exclude, 'tests/**/*.spec.ts'],
  },
})
