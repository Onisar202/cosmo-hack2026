import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useCalculation } from '../src/api/useCalculation'
import type { CalculationRequest, CalculationResult, TaskStatusResponse } from '../src/api/types'

const createCalculation = vi.fn()
const getCalculationStatus = vi.fn()
const getResult = vi.fn()

// vi.mock — поднимается над импортами самим vitest, так что useCalculation.ts
// (импортирующий эти функции из './client') получает моки, а не реальный fetch.
vi.mock('../src/api/client', () => ({
  createCalculation: (...args: unknown[]) => createCalculation(...(args as [])),
  getCalculationStatus: (...args: unknown[]) => getCalculationStatus(...(args as [])),
  getResult: (...args: unknown[]) => getResult(...(args as [])),
  ApiRequestError: class ApiRequestError extends Error {
    code = 'mock_error'
    httpStatus = 500
  },
}))

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

const BASE_REQUEST: CalculationRequest = {
  mode: 'current',
  start_at: '2024-05-10T12:00:00Z',
  duration_hours: 4,
  search_window_hours: 8,
}

function fakeResult(id: string): CalculationResult {
  return {
    result_id: id,
    computed_at: '2024-05-10T12:00:00Z',
    request: BASE_REQUEST,
    mode: 'current',
    as_of: null,
    algorithm_version: '0.1.0',
    data_manifest: [],
    orbit: {
      source: 'celestrak',
      norad_id: '25544',
      elements_epoch: '2024-05-10T12:00:00Z',
      elements_age_hours: 0,
      coordinate_system: 'TEME',
      is_reconstructed: false,
      record_id: 'rec-orbit-1',
    },
    windows: [],
    coverage: { requested_period_supported: true, archive_gaps: [] },
    limitations: [],
    warnings: [],
    recommendation: { status: 'tie', window_id: null, explanation: '' },
    source_status: [],
  }
}

/** Прокачивает несколько микрозадач подряд — достаточно, чтобы промис-цепочка
 * внутри useCalculation.run() продвинулась на очередной `await`. */
async function flushMicrotasks(times = 5) {
  for (let i = 0; i < times; i += 1) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

describe('useCalculation — гонка запросов', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    createCalculation.mockReset()
    getCalculationStatus.mockReset()
    getResult.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it(
    'ответ устаревшего (первого) запроса, пришедший ПОСЛЕ второго, никогда не перекрывает ' +
      'уже показанный результат более нового запроса (.ai/frontend-prompt.md «Работа с API»)',
    async () => {
      const createCalcCalls = [
        deferred<{ task_id: string; status: 'pending' }>(),
        deferred<{ task_id: string; status: 'pending' }>(),
      ]
      let createCalcIndex = 0
      createCalculation.mockImplementation(() => createCalcCalls[createCalcIndex++].promise)

      const statusByTask: Record<string, Deferred<TaskStatusResponse>> = {
        'task-a': deferred<TaskStatusResponse>(),
        'task-b': deferred<TaskStatusResponse>(),
      }
      getCalculationStatus.mockImplementation((taskId: string) => statusByTask[taskId].promise)

      const resultById: Record<string, Deferred<CalculationResult>> = {
        'result-a': deferred<CalculationResult>(),
        'result-b': deferred<CalculationResult>(),
      }
      getResult.mockImplementation((resultId: string) => resultById[resultId].promise)

      const { result } = renderHook(() => useCalculation())

      // Пользователь быстро меняет параметры и запускает расчёт дважды —
      // запрос A уходит первым, запрос B почти сразу следом.
      act(() => {
        result.current.run(BASE_REQUEST)
      })
      act(() => {
        result.current.run({ ...BASE_REQUEST, duration_hours: 5 })
      })

      // Сеть отвечает не по порядку отправки: B создаётся раньше, чем A.
      createCalcCalls[1].resolve({ task_id: 'task-b', status: 'pending' })
      await flushMicrotasks()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000)
      })

      statusByTask['task-b'].resolve({
        task_id: 'task-b',
        status: 'done',
        result_id: 'result-b',
        error: null,
      })
      await flushMicrotasks()
      resultById['result-b'].resolve(fakeResult('result-b'))
      await flushMicrotasks()

      expect(result.current.state.phase).toBe('done')
      if (result.current.state.phase === 'done') {
        expect(result.current.state.result.result_id).toBe('result-b')
      }

      // Только теперь опаздавший ответ на A наконец приходит — целиком, до
      // самого результата — и не должен ничего изменить на экране.
      createCalcCalls[0].resolve({ task_id: 'task-a', status: 'pending' })
      await flushMicrotasks()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000)
      })
      statusByTask['task-a'].resolve({
        task_id: 'task-a',
        status: 'done',
        result_id: 'result-a',
        error: null,
      })
      await flushMicrotasks()
      resultById['result-a'].resolve(fakeResult('result-a'))
      await flushMicrotasks()

      expect(result.current.state.phase).toBe('done')
      if (result.current.state.phase === 'done') {
        expect(result.current.state.result.result_id).toBe('result-b')
      }
    },
  )

  it('markStale помечает уже показанный результат, не подменяя его новым (форма изменилась)', async () => {
    createCalculation.mockResolvedValue({ task_id: 'task-only', status: 'pending' })
    getCalculationStatus.mockResolvedValue({
      task_id: 'task-only',
      status: 'done',
      result_id: 'result-only',
      error: null,
    })
    getResult.mockResolvedValue(fakeResult('result-only'))

    const { result } = renderHook(() => useCalculation())

    act(() => {
      result.current.run(BASE_REQUEST)
    })
    await flushMicrotasks()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    await flushMicrotasks()

    expect(result.current.state.phase).toBe('done')

    act(() => {
      result.current.markStale()
    })

    expect(result.current.state.phase).toBe('done')
    if (result.current.state.phase === 'done') {
      expect(result.current.state.result.result_id).toBe('result-only')
      expect(result.current.state.stale).toBe(true)
    }
  })
})
