import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { expect, test, type Page } from '@playwright/test'
import type { CalculationResult, SourceStatus, TaskStatusResponse } from '../src/api/types'
import { beyondHorizonResult, conflictResult } from './fixtures/uiStates'

// Загружается через fs, а не `import ... from '...json'` — раннер
// Playwright исполняет спеки нативным Node ESM-загрузчиком без резолвинга
// JSON-модулей (в отличие от Vite/vitest, которые понимают это из коробки).
const successFixture = JSON.parse(
  readFileSync(
    fileURLToPath(new URL('../src/api/fixtures/success.json', import.meta.url)),
    'utf-8',
  ),
) as CalculationResult

/**
 * FN-35: UI-путь сравнения окон, неполноты и пересчёта плана, покрытый
 * Playwright'ом БЕЗ живого бэкенда — в отличие от `web/tests/stage1.spec.ts`
 * (FN-28), которая намеренно бьёт по развёрнутому стеку (CelesTrak/NOAA SWPC
 * за сетью) и поэтому не входит в CI (`.github/workflows/ci.yml`:
 * `frontend-checks` гоняет только lint/format/test/build). Здесь весь
 * контракт `src/api/client.ts` (`POST /api/calculations`,
 * `GET /api/calculations/:id`, `GET /api/results/:id`,
 * `GET /api/sources/status`) перехватывается через `page.route`, поэтому
 * проверка воспроизводима без сети и годится для более широкого запуска —
 * см. также требование acceptance criterion 5 (успешный путь, конфликт,
 * отказ источника, гонка запросов).
 *
 * Два состояния (конфликт механизмов и `beyond_horizon`) не входят в четыре
 * канонические фикстуры контракта (`web/src/api/fixtures.ts` — инвариант
 * «ровно 4 фикстуры» не трогается) и заведены отдельно в
 * `web/tests/fixtures/uiStates.ts` только для этого файла.
 */

async function mockSourcesStatus(page: Page, items: SourceStatus[] = successFixture.source_status) {
  await page.route('**/api/sources/status', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(items) }),
  )
}

/** Полный цикл одного расчёта: POST создаёт задачу, один опрос статуса сразу
 * возвращает `done`, дальше — сохранённый результат (контракт
 * `web/src/api/useCalculation.ts`: pending -> poll -> done -> getResult). */
async function mockSingleCalculation(
  page: Page,
  options: { taskId: string; result: CalculationResult },
) {
  const { taskId, result } = options
  await page.route('**/api/calculations', (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ task_id: taskId, status: 'pending' }),
    })
  })
  await page.route(`**/api/calculations/${taskId}`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        task_id: taskId,
        status: 'done',
        result_id: result.result_id,
        error: null,
      } satisfies TaskStatusResponse),
    }),
  )
  await page.route(`**/api/results/${result.result_id}`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(result) }),
  )
}

async function fillForm(page: Page, values: { durationHours: string; searchWindowHours: string }) {
  await page.getByLabel('Длительность ВКД, ч').fill(values.durationHours)
  await page.getByLabel('Период поиска начала, ч').fill(values.searchWindowHours)
}

async function fillAndSubmit(
  page: Page,
  values: { durationHours: string; searchWindowHours: string },
) {
  await fillForm(page, values)
  await page.getByRole('button', { name: 'Рассчитать' }).click()
}

/**
 * `CalculationPanel` deliberately disables «Рассчитать» while a calculation
 * is `pending`/`running` — a real user cannot fire a second overlapping
 * submit through it, by design. That UX guard is not what request-race
 * protection (`useCalculation.ts`, `web/tests/requestRace.test.tsx`) exists
 * to cover — it's the defence-in-depth for cases the button can't prevent
 * (slow/duplicate responses, multiple tabs). `requestSubmit()` fires the
 * exact same `onSubmit` -> `RequestForm.handleSubmit` -> `run()` path a
 * click would, without depending on the button's disabled attribute, so the
 * second submission below still exercises the real form/DOM/hook.
 */
async function submitFormDirectly(page: Page) {
  await page.evaluate(() => {
    document.querySelector('form')?.requestSubmit()
  })
}

test.describe('FN-35: сравнение окон, неполнота, пересчёт (мок HTTP, без бэкенда)', () => {
  test('успешный путь: рекомендация и объяснение выбранного окна видны', async ({ page }) => {
    await mockSourcesStatus(page)
    await mockSingleCalculation(page, { taskId: 'task-success', result: successFixture })

    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Параметры ВКД' })).toBeVisible()

    await fillAndSubmit(page, { durationHours: '6', searchWindowHours: '6' })

    await expect(page.getByText('Окно выбрано')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText(successFixture.recommendation.explanation)).toBeVisible()
    // Заголовки карточек окон, а не совпадающие по подстроке чипы источников
    // предупреждений (WarningsList рендерит `окно win-b` отдельным чипом).
    await expect(page.getByRole('heading', { name: 'Окно win-a' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Окно win-b' })).toBeVisible()
    await expect(page.getByText('рекомендовано')).toBeVisible()
  })

  test('конфликт механизмов: "оснований недостаточно", не "равнозначны" — оба окна видны', async ({
    page,
  }) => {
    await mockSourcesStatus(page)
    await mockSingleCalculation(page, { taskId: 'task-conflict', result: conflictResult })

    await page.goto('/')
    await fillAndSubmit(page, { durationHours: '4', searchWindowHours: '8' })

    await expect(page.getByText('Оснований для рекомендации недостаточно')).toBeVisible({
      timeout: 15_000,
    })
    // Не "равнозначны" (tie — про равенство) и не "выбрано" (selected) — это
    // конфликт правила доминирования v2 (§11 правило 3), другой смысл.
    await expect(page.getByText('Окна равнозначны')).toHaveCount(0)
    await expect(page.getByText('Окно выбрано')).toHaveCount(0)

    // Оба окна остаются видимыми, ни одно не скрыто (.ai/main-prompt.md §11
    // правило 1 / §12 «все окна остаются видимыми»).
    await expect(page.getByRole('heading', { name: 'Окно win-a' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Окно win-b' })).toBeVisible()
    await expect(page.getByText('исключено из сравнения', { exact: true })).toHaveCount(0)
  })

  test('источник недоступен: понятная ошибка с кодом и предложением повторить, без ложного "спокойно"', async ({
    page,
  }) => {
    await mockSourcesStatus(page)
    await page.route('**/api/calculations', (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      return route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({
          error: { code: 'orbit_error_source', message: 'CelesTrak недоступен: таймаут запроса.' },
        }),
      })
    })

    await page.goto('/')
    await fillAndSubmit(page, { durationHours: '4', searchWindowHours: '8' })

    await expect(page.getByText('Расчёт завершился ошибкой (orbit_error_source)')).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText('CelesTrak недоступен: таймаут запроса.')).toBeVisible()
    await expect(
      page.getByText('Измените параметры или повторите запрос — задачу можно запустить заново.'),
    ).toBeVisible()

    // Никакого окна/рекомендации, как будто расчёт прошёл спокойно.
    await expect(page.getByText('Окно выбрано')).toHaveCount(0)
    await expect(page.getByText(/Окно win-/)).toHaveCount(0)
  })

  test('за горизонтом прогноза: "не покрыто" отличимо от "спокойно", окно исключено с явной причиной', async ({
    page,
  }) => {
    await mockSourcesStatus(page)
    await mockSingleCalculation(page, { taskId: 'task-horizon', result: beyondHorizonResult })

    await page.goto('/')
    await fillAndSubmit(page, { durationHours: '3', searchWindowHours: '12' })

    await expect(page.getByText('за горизонтом прогноза')).toBeVisible({ timeout: 15_000 })
    // Отличимо иконкой+текстом от "оценка выполнена" (фон/спокойно) — не тот
    // же статус, что "background"/"фон".
    await expect(page.getByText('оценка выполнена').first()).toBeVisible()
    await expect(page.getByText('исключено из сравнения', { exact: true })).toBeVisible()
    await expect(page.getByText(/интервал окна вне горизонта прогноза/).first()).toBeVisible()
  })

  test('гонка запросов: второй расчёт с другими параметрами не перекрывается опоздавшим первым', async ({
    page,
  }) => {
    await mockSourcesStatus(page)

    const slowResult: CalculationResult = {
      ...successFixture,
      result_id: 'res-ui-race-slow',
    }
    const fastResult: CalculationResult = {
      ...conflictResult,
      result_id: 'res-ui-race-fast',
    }

    let createCalls = 0
    await page.route('**/api/calculations', (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      createCalls += 1
      const taskId = createCalls === 1 ? 'task-race-slow' : 'task-race-fast'
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ task_id: taskId, status: 'pending' }),
      })
    })
    // Первый (устаревший) запрос отвечает на опрос статуса намного позже
    // второго — имитирует опоздавший ответ гонки запросов.
    await page.route('**/api/calculations/task-race-slow', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 4_000))
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          task_id: 'task-race-slow',
          status: 'done',
          result_id: slowResult.result_id,
          error: null,
        } satisfies TaskStatusResponse),
      })
    })
    await page.route('**/api/calculations/task-race-fast', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          task_id: 'task-race-fast',
          status: 'done',
          result_id: fastResult.result_id,
          error: null,
        } satisfies TaskStatusResponse),
      }),
    )
    await page.route(`**/api/results/${slowResult.result_id}`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(slowResult),
      }),
    )
    await page.route(`**/api/results/${fastResult.result_id}`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(fastResult),
      }),
    )

    await page.goto('/')

    // Первый запрос уходит и успевает встать в опрос статуса (POLL_INTERVAL_MS
    // = 1200 мс в useCalculation.ts) до того, как уйдёт второй — так второй
    // застаёт первый уже "в полёте" (его GET .../task-race-slow уже висит в
    // ожидании искусственной задержки), а не отменяет его на старте create.
    await fillAndSubmit(page, { durationHours: '2', searchWindowHours: '6' })
    await expect(page.getByText('task_id: task-race-slow', { exact: false })).toBeVisible()
    await page.waitForTimeout(1_500)

    // Кнопка «Рассчитать» отключена, пока первый расчёт не завершён (см.
    // submitFormDirectly) — второй запрос уходит через прямой submit формы.
    await fillForm(page, { durationHours: '3', searchWindowHours: '6' })
    await submitFormDirectly(page)

    // Второй (последний) результат — единственный, что показан.
    await expect(page.getByText('Результат res-ui-race-fast')).toBeVisible({ timeout: 15_000 })

    // Ждём дольше, чем задержка опоздавшего первого ответа (4 с), и
    // убеждаемся, что он так и не перекрыл экран.
    await page.waitForTimeout(4_500)
    await expect(page.getByText('Результат res-ui-race-fast')).toBeVisible()
    await expect(page.getByText('Результат res-ui-race-slow')).toHaveCount(0)
  })
})
