import { expect, test } from '@playwright/test'

// Единственные коды ошибок, которые эта проверка принимает как «реальный
// источник недоступен из текущей сети» (совпадает с
// tests/integration/test_stage1.py:
// SOURCE_UNAVAILABLE_ERROR_CODES — src/api/service.py:
// `f"orbit_{orbit_fetch.outcome}"`, outcome ∈ {"error_source", "error_quota"}).
// Любой другой код (result_schema_violation, orbit_propagation_failed,
// internal_error и т.п.) — дефект развёртывания/сервиса, а не отсутствие
// сети, и обязан ронять проверку, а не маскироваться под неё (round 1
// ревью PR #21).
const SOURCE_UNAVAILABLE_ERROR_CODES = new Set(['orbit_error_source', 'orbit_error_quota'])

/**
 * FN-28 (S1-10): сквозная UI-проверка развёрнутого этапа —
 * UI → API → source → store → orbit → result, тем же путём, каким им
 * воспользуется человек (.ai/main-prompt.md §12 «самостоятельный
 * пользовательский путь»). В отличие от web/tests/*.test.tsx (vitest,
 * jsdom, API замокан) здесь браузер обращается к реально запущенному
 * стеку — см. корневой README.md «Развёртывание» для запуска и
 * `playwright.config.ts` для базового URL.
 *
 * `mode=current` дергает настоящие CelesTrak/NOAA SWPC (main-prompt.md §11
 * «получение реального источника» — часть приёмки этого этапа). Если сеть,
 * в которой выполняется проверка, их не пропускает (см. README.md
 * «Известное ограничение окружения сборки»), сервис отвечает честной
 * ошибкой, а не выдуманным результатом (main-prompt.md §2) — интерфейс
 * обязан показать эту ошибку текстом, что и проверяется ниже независимо от
 * исхода сетевого запроса.
 */

test.describe('FN-28: сквозная проверка развёрнутого стека', () => {
  test('панель источников показывает реальные статусы, а не заглушку', async ({ page }) => {
    await page.goto('/')

    await expect(page.getByRole('heading', { name: 'Источники данных' })).toBeVisible()
    // Источники из sources.yaml (FN-22/FN-24) видны по source_id — панель не
    // пустая заглушка, а реальный ответ GET /api/sources/status.
    await expect(page.getByText('celestrak-gp')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('noaa-swpc-proton-flux')).toBeVisible()
  })

  test('расчёт mode=current: результат виден в интерфейсе и в «Сохранённые результаты», либо честная ошибка источника', async ({
    page,
  }) => {
    await page.goto('/')

    await expect(page.getByRole('heading', { name: 'Параметры ВКД' })).toBeVisible()
    await page.getByRole('button', { name: 'Рассчитать' }).click()

    const errorAlert = page.getByText('Расчёт завершился ошибкой', { exact: false })
    const orbitCard = page.getByText(/Орбита МКС \(NORAD 25544\)/)

    // Долгая операция показывает статус, а не зависает молча (main-prompt.md
    // §12) — ждём терминального состояния формы: либо результат, либо ошибка.
    await expect(errorAlert.or(orbitCard)).toBeVisible({ timeout: 45_000 })

    if (await errorAlert.isVisible()) {
      // Код ошибки виден пользователю текстом в самой форме — «Расчёт
      // завершился ошибкой ({code})» (web/src/components/CalculationPanel.tsx)
      // — извлекаем из него, а не гадаем по факту наличия алерта.
      const alertText = (await errorAlert.textContent()) ?? ''
      const codeMatch = alertText.match(/\(([\w-]+)\)/)
      const code = codeMatch?.[1]

      if (!code || !SOURCE_UNAVAILABLE_ERROR_CODES.has(code)) {
        throw new Error(
          'Расчёт завершился ошибкой, код которой НЕ входит в список ожидаемых ' +
            `признаков недоступности источника (${[...SOURCE_UNAVAILABLE_ERROR_CODES].join(', ')}) ` +
            `— похоже на дефект развёртывания/сервиса, а не отсутствие сети. ` +
            `Текст ошибки: ${alertText}`,
        )
      }

      test.skip(
        true,
        `mode=current завершился ошибкой реального источника (код ${code}, ` +
          'ожидаемо без сетевого доступа к CelesTrak/NOAA SWPC из этой сети) — ' +
          'см. README.md «Известное ограничение окружения сборки». Форма честно ' +
          'показала ошибку, а не благоприятную оценку — это и была проверяемая часть.',
      )
    }

    await expect(orbitCard).toBeVisible()
    await expect(page.getByText('CelesTrak (текущие элементы)')).toBeVisible()
    // Оба обязательных механизма воздействия ещё не реализованы на этом
    // этапе (FN-26/FN-25/FN-22 — интерпретация приходит позже) — интерфейс
    // показывает это честно, а не имитирует оценку.
    await expect(page.getByText('механизм ещё не реализован').first()).toBeVisible()

    // Тот же сохранённый объект доступен из «Сохранённые результаты»
    // (main-prompt.md §3 «интерфейс и обе выгрузки читают один и тот же
    // сохранённый объект») — переключение вкладки идёт через реальный
    // GET /api/results, а не по локальному состоянию формы.
    await page.getByRole('tab', { name: 'Сохранённые результаты' }).click()
    await expect(page.getByText('Последние сохранённые расчёты')).toBeVisible()
    const savedItem = page.getByText(/— current/).first()
    await expect(savedItem).toBeVisible({ timeout: 15_000 })
    await savedItem.click()
    await expect(page.getByText(/Орбита МКС \(NORAD 25544\)/)).toBeVisible()
  })
})
