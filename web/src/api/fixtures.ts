/**
 * Снимок четырёх демонстрационных фикстур `contracts/fixtures/*.json` для
 * панели демо-режима (.ai/frontend-prompt.md: «каждый экран проверяется на
 * всех четырёх фикстурах»). Источник истины по форме — `contracts/`; при
 * изменении контракта эти файлы обновляются вместе с оригиналами
 * (contracts/README.md).
 */
import equalWindows from './fixtures/equal-windows.json'
import incomplete from './fixtures/incomplete.json'
import sourceError from './fixtures/source-error.json'
import success from './fixtures/success.json'
import type { CalculationResult } from './types'

export interface DemoFixture {
  id: string
  label: string
  description: string
  result: CalculationResult
}

export const DEMO_FIXTURES: DemoFixture[] = [
  {
    id: 'success',
    label: 'Успешный расчёт',
    description:
      'historical_forecast, два окна: окно A доминирует над B по правилу доминирования v2 (не хуже по обоим механизмам, устойчиво лучше по механизму 1).',
    result: success as CalculationResult,
  },
  {
    id: 'incomplete',
    label: 'Неполные данные',
    description:
      'MMOD ещё не реализован, по механизму 1 в одном из окон нет данных — оба окна исключены из сравнения.',
    result: incomplete as CalculationResult,
  },
  {
    id: 'source-error',
    label: 'Ошибка источника',
    description: 'Архив источника космической погоды отвечает 429 — отказ не подменяется «фоном».',
    result: sourceError as CalculationResult,
  },
  {
    id: 'equal-windows',
    label: 'Равнозначные окна',
    description:
      'Оба окна равнозначны по правилу доминирования v2 — рекомендация не выдаётся (tie).',
    result: equalWindows as CalculationResult,
  },
]

export function getFixtureById(id: string): DemoFixture | undefined {
  return DEMO_FIXTURES.find((fixture) => fixture.id === id)
}
