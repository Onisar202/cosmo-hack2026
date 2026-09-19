import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiRequestError, createCalculation, getCalculationStatus, getResult } from './client'
import type { ApiErrorBody, CalculationRequest, CalculationResult } from './types'

const POLL_INTERVAL_MS = 1200

export type CalculationState =
  | { phase: 'idle' }
  | { phase: 'pending' | 'running'; taskId: string | null }
  | { phase: 'done'; taskId: string; result: CalculationResult; stale: boolean }
  | { phase: 'failed'; taskId: string | null; error: ApiErrorBody; stale: boolean }

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const timer = setTimeout(resolve, ms)
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer)
        reject(new DOMException('Aborted', 'AbortError'))
      },
      { once: true },
    )
  })
}

/**
 * Оркестрация запуск -> опрос статуса -> сохранённый результат.
 *
 * Ответы устаревших запросов никогда не перекрывают более новые
 * (.ai/frontend-prompt.md «Работа с API»): каждый вызов `run` заводит новое
 * поколение (`seqRef`) и свой `AbortController`; любой ответ, пришедший не
 * от актуального поколения, отбрасывается молча, а не дорисовывается поверх.
 */
export function useCalculation() {
  const [state, setState] = useState<CalculationState>({ phase: 'idle' })
  const seqRef = useRef(0)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    return () => {
      abortRef.current?.abort()
    }
  }, [])

  const run = useCallback((payload: CalculationRequest) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const mySeq = ++seqRef.current
    const isCurrent = () => seqRef.current === mySeq

    setState({ phase: 'pending', taskId: null })

    void (async () => {
      try {
        const created = await createCalculation(payload, controller.signal)
        if (!isCurrent()) return
        setState({ phase: 'pending', taskId: created.task_id })

        for (;;) {
          await sleep(POLL_INTERVAL_MS, controller.signal)
          if (!isCurrent()) return

          const status = await getCalculationStatus(created.task_id, controller.signal)
          if (!isCurrent()) return

          if (status.status === 'pending' || status.status === 'running') {
            setState({ phase: status.status, taskId: created.task_id })
            continue
          }

          if (status.status === 'failed') {
            setState({
              phase: 'failed',
              taskId: created.task_id,
              error: status.error ?? {
                code: 'unknown_error',
                message: 'Задача завершилась ошибкой без описания.',
              },
              stale: false,
            })
            return
          }

          // status.status === 'done'
          const result = await getResult(status.result_id as string, controller.signal)
          if (!isCurrent()) return
          setState({ phase: 'done', taskId: created.task_id, result, stale: false })
          return
        }
      } catch (err) {
        if (controller.signal.aborted || !isCurrent()) return
        const message =
          err instanceof ApiRequestError || err instanceof Error
            ? err.message
            : 'Неизвестная ошибка при обращении к сервису.'
        const code = err instanceof ApiRequestError ? err.code : 'client_error'
        setState({ phase: 'failed', taskId: null, error: { code, message }, stale: false })
      }
    })()
  }, [])

  /** Форма изменилась под уже показанным результатом: он относится к
   * прежним параметрам, пока пользователь не пересчитает заново. */
  const markStale = useCallback(() => {
    setState((prev) =>
      prev.phase === 'done' || prev.phase === 'failed' ? { ...prev, stale: true } : prev,
    )
  }, [])

  const reset = useCallback(() => {
    abortRef.current?.abort()
    seqRef.current += 1
    setState({ phase: 'idle' })
  }, [])

  return { state, run, markStale, reset }
}
