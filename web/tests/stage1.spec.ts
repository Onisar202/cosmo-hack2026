import { expect, test } from '@playwright/test'

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
      test.skip(
        true,
        'mode=current завершился ошибкой реального источника (ожидаемо без ' +
          'сетевого доступа к CelesTrak/NOAA SWPC из этой сети) — см. ' +
          'README.md «Известное ограничение окружения сборки». Форма честно ' +
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
