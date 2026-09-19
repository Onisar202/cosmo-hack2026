/**
 * Тонкий HTTP-клиент к расчётному API S1-07 (`contracts/README.md`, раздел
 * «Endpoint shapes для S1-07»). Не интерпретирует ответы — только разбирает
 * HTTP и типизирует тело (см. src/api/types.ts).
 */
import type {
  ApiErrorBody,
  CalculationRequest,
  CalculationResult,
  Mode,
  ResultListItem,
  SourceStatus,
  TaskCreatedResponse,
  TaskStatusResponse,
} from './types'

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '/api'

/** Сервис ответил структурированной ошибкой `{ error: { code, message } }`. */
export class ApiRequestError extends Error {
  readonly code: string
  readonly httpStatus: number

  constructor(httpStatus: number, body: ApiErrorBody) {
    super(body.message)
    this.name = 'ApiRequestError'
    this.code = body.code
    this.httpStatus = httpStatus
  }
}

/** Сеть недоступна или ответ не удалось разобрать — не спутано с осмысленной
 * ошибкой сервиса (ApiRequestError). */
export class NetworkError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options)
    this.name = 'NetworkError'
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE_URL}${path}`, init)
  } catch (cause) {
    if (init.signal?.aborted) {
      // Отменённый запрос — не сетевая ошибка для пользователя, а ожидаемый
      // побочный эффект гонки запросов (см. src/api/useCalculation ниже).
      throw cause
    }
    throw new NetworkError('Не удалось обратиться к сервису расчёта.', { cause })
  }

  if (!response.ok) {
    let body: { error?: ApiErrorBody } | null
    try {
      body = (await response.json()) as { error?: ApiErrorBody }
    } catch {
      body = null
    }
    if (body?.error) {
      throw new ApiRequestError(response.status, body.error)
    }
    throw new ApiRequestError(response.status, {
      code: 'unknown_error',
      message: `Сервис ответил кодом ${response.status} без структурированной ошибки.`,
    })
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export function createCalculation(
  payload: CalculationRequest,
  signal?: AbortSignal,
): Promise<TaskCreatedResponse> {
  return request<TaskCreatedResponse>('/calculations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
}

export function getCalculationStatus(
  taskId: string,
  signal?: AbortSignal,
): Promise<TaskStatusResponse> {
  return request<TaskStatusResponse>(`/calculations/${encodeURIComponent(taskId)}`, { signal })
}

export function getResult(resultId: string, signal?: AbortSignal): Promise<CalculationResult> {
  return request<CalculationResult>(`/results/${encodeURIComponent(resultId)}`, { signal })
}

/**
 * Прямая ссылка на скачивание сохранённого результата (FN-36, S2-06) —
 * не проходит через `request()`: ответ здесь не JSON-тело для разбора, а
 * файл, который браузер должен сохранить/открыть сам (интерфейс использует
 * `<a href={...} download>`, а не `fetch`).
 */
export function getResultExportUrl(resultId: string, format: 'json' | 'html'): string {
  return `${BASE_URL}/results/${encodeURIComponent(resultId)}/export.${format}`
}

export function listResults(
  params: { mode?: Mode; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<ResultListItem[]> {
  const query = new URLSearchParams()
  if (params.mode) query.set('mode', params.mode)
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.offset != null) query.set('offset', String(params.offset))
  const qs = query.toString()
  return request<ResultListItem[]>(`/results${qs ? `?${qs}` : ''}`, { signal })
}

export function refreshSources(signal?: AbortSignal): Promise<SourceStatus[]> {
  return request<SourceStatus[]>('/sources/refresh', { method: 'POST', signal })
}

export function getSourcesStatus(signal?: AbortSignal): Promise<SourceStatus[]> {
  return request<SourceStatus[]>('/sources/status', { signal })
}
