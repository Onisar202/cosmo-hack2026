/**
 * Единственный модуль, где что-либо делается со временем (.ai/frontend-prompt.md,
 * «Время и часовые пояса»). Прямые вызовы `new Date(...).toString()` или
 * `toLocaleString()` без явной таймзоны в компонентах запрещены — браузер
 * подставит таймзону машины показа, и время разъедется на чужом ноутбуке.
 *
 * Времена из API — строки ISO 8601 с явным смещением и хранятся строками до
 * момента отображения (round-trip через Date теряет иначе ничего не теряет
 * здесь, но избегаем его вне этого модуля, чтобы вся логика времени была в
 * одном месте).
 */

const MONTH_DAY_PAD = 2

function pad(value: number, size: number = MONTH_DAY_PAD): string {
  return String(value).padStart(size, '0')
}

/** Разбирает ISO-строку из API. Бросает исключение на невалидный/naive ввод. */
export function parseIsoUtc(iso: string): Date {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) {
    throw new Error(`invalid ISO 8601 datetime: ${iso}`)
  }
  return date
}

export function isValidIsoUtc(iso: string): boolean {
  try {
    parseIsoUtc(iso)
    return true
  } catch {
    return false
  }
}

/**
 * Форматирует ISO-строку в UTC: "2024-05-10 12:00:00 UTC". Единственное
 * место, которому разрешено читать компоненты даты (через getUTC*, никогда
 * через локальные get* или toLocaleString/toString).
 */
export function formatUtc(iso: string, options: { withSeconds?: boolean } = {}): string {
  const { withSeconds = true } = options
  const date = parseIsoUtc(iso)
  const y = date.getUTCFullYear()
  const mo = pad(date.getUTCMonth() + 1)
  const d = pad(date.getUTCDate())
  const h = pad(date.getUTCHours())
  const mi = pad(date.getUTCMinutes())
  const s = pad(date.getUTCSeconds())
  const time = withSeconds ? `${h}:${mi}:${s}` : `${h}:${mi}`
  return `${y}-${mo}-${d} ${time} UTC`
}

function pluralizeRu(n: number, forms: [one: string, few: string, many: string]): string {
  const mod10 = n % 10
  const mod100 = n % 100
  if (mod10 === 1 && mod100 !== 11) return forms[0]
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1]
  return forms[2]
}

/**
 * Давность момента относительно `nowIso` (по умолчанию — текущее время):
 * "получено 42 минуты назад". Не использует локальные таймзоны — оперирует
 * только разницей в миллисекундах между двумя UTC-моментами.
 */
export function formatAgo(iso: string, nowIso: string = new Date().toISOString()): string {
  const then = parseIsoUtc(iso).getTime()
  const now = parseIsoUtc(nowIso).getTime()
  const diffMs = now - then
  if (diffMs < 0) {
    return 'в будущем'
  }

  const minutes = Math.floor(diffMs / 60_000)
  if (minutes < 1) return 'меньше минуты назад'
  if (minutes < 60) {
    return `${minutes} ${pluralizeRu(minutes, ['минуту', 'минуты', 'минут'])} назад`
  }

  const hours = Math.floor(minutes / 60)
  if (hours < 24) {
    return `${hours} ${pluralizeRu(hours, ['час', 'часа', 'часов'])} назад`
  }

  const days = Math.floor(hours / 24)
  return `${days} ${pluralizeRu(days, ['день', 'дня', 'дней'])} назад`
}

/** "6 ч" / "1 ч" — короткая подпись длительности в часах для формы и сводок. */
export function formatDurationHours(hours: number): string {
  const rounded = Math.round(hours * 100) / 100
  return `${rounded} ч`
}

/**
 * ISO UTC -> значение для `<input type="datetime-local">`. Поле формы
 * трактуется как UTC "как есть" (без подстановки таймзоны браузера) —
 * подписано в интерфейсе явно как UTC, чтобы не изобретать второй часовой
 * пояс поверх уже вызывающего путаницу расчёта (.ai/frontend-prompt.md п.2).
 */
export function utcInputValueFromIso(iso: string): string {
  const date = parseIsoUtc(iso)
  const y = date.getUTCFullYear()
  const mo = pad(date.getUTCMonth() + 1)
  const d = pad(date.getUTCDate())
  const h = pad(date.getUTCHours())
  const mi = pad(date.getUTCMinutes())
  return `${y}-${mo}-${d}T${h}:${mi}`
}

/** Обратное преобразование: значение поля формы (трактуется как UTC) -> ISO. */
export function isoFromUtcInputValue(value: string): string | null {
  if (!value) return null
  // datetime-local отдаёт "YYYY-MM-DDTHH:mm" (без секунд/смещения).
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})$/.exec(value)
  if (!match) return null
  const [, datePart, timePart] = match
  const iso = `${datePart}T${timePart}:00Z`
  return isValidIsoUtc(iso) ? iso : null
}

/** Текущий момент как ISO UTC-строка. `toISOString()` всегда в UTC — не
 * подпадает под запрет на toLocaleString/toString с локальной таймзоной. */
export function nowIso(): string {
  return new Date().toISOString()
}

/** ISO UTC -> количество часов между двумя моментами (для валидации формы). */
export function diffHours(fromIso: string, toIso: string): number {
  const from = parseIsoUtc(fromIso).getTime()
  const to = parseIsoUtc(toIso).getTime()
  return (to - from) / 3_600_000
}

export function addHoursIso(iso: string, hours: number): string {
  const date = parseIsoUtc(iso)
  return new Date(date.getTime() + hours * 3_600_000).toISOString()
}
