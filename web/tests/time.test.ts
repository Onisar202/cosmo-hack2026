import { describe, expect, it } from 'vitest'
import {
  addHoursIso,
  diffHours,
  formatAgo,
  formatUtc,
  isoFromUtcInputValue,
  isValidIsoUtc,
  utcInputValueFromIso,
} from '../src/utils/time'

describe('formatUtc', () => {
  it('formats an ISO string as UTC with an explicit "UTC" suffix, never the local timezone', () => {
    expect(formatUtc('2024-05-10T12:00:00Z')).toBe('2024-05-10 12:00:00 UTC')
  })

  it('normalizes a non-zero offset into UTC components (05:30 at +05:30 is 00:00Z)', () => {
    expect(formatUtc('2024-05-10T05:30:00+05:30')).toBe('2024-05-10 00:00:00 UTC')
  })

  it('can omit seconds', () => {
    expect(formatUtc('2024-05-10T12:00:00Z', { withSeconds: false })).toBe('2024-05-10 12:00 UTC')
  })

  it('rejects an invalid datetime string', () => {
    expect(() => formatUtc('not-a-date')).toThrow()
  })
})

describe('isValidIsoUtc', () => {
  it('accepts a valid ISO 8601 string', () => {
    expect(isValidIsoUtc('2024-05-10T12:00:00Z')).toBe(true)
  })

  it('rejects garbage input', () => {
    expect(isValidIsoUtc('nope')).toBe(false)
  })
})

describe('formatAgo', () => {
  const now = '2024-05-10T12:00:00Z'

  it('reports "less than a minute ago" for a very recent moment', () => {
    expect(formatAgo('2024-05-10T11:59:45Z', now)).toBe('меньше минуты назад')
  })

  it('pluralizes minutes correctly (42 -> минуты, not минут)', () => {
    expect(formatAgo('2024-05-10T11:18:00Z', now)).toBe('42 минуты назад')
  })

  it('pluralizes minutes correctly (1 -> минуту)', () => {
    expect(formatAgo('2024-05-10T11:59:00Z', now)).toBe('1 минуту назад')
  })

  it('switches to hours past 60 minutes', () => {
    expect(formatAgo('2024-05-10T09:00:00Z', now)).toBe('3 часа назад')
  })

  it('switches to days past 24 hours', () => {
    expect(formatAgo('2024-05-08T12:00:00Z', now)).toBe('2 дня назад')
  })

  it('never reports a negative age as "ago"', () => {
    expect(formatAgo('2024-05-10T13:00:00Z', now)).toBe('в будущем')
  })
})

describe('datetime-local <-> ISO UTC round-trip', () => {
  it('renders the UTC wall-clock components into the input value, not the local ones', () => {
    expect(utcInputValueFromIso('2024-05-10T05:30:00+05:30')).toBe('2024-05-10T00:00')
  })

  it('treats an input value as UTC directly when converting back to ISO', () => {
    expect(isoFromUtcInputValue('2024-05-10T00:00')).toBe('2024-05-10T00:00:00Z')
  })

  it('round-trips without drifting', () => {
    const iso = '2024-06-15T08:30:00Z'
    expect(isoFromUtcInputValue(utcInputValueFromIso(iso))).toBe(iso)
  })

  it('rejects a malformed input value instead of guessing', () => {
    expect(isoFromUtcInputValue('not-a-datetime')).toBeNull()
  })
})

describe('diffHours / addHoursIso', () => {
  it('computes the number of hours between two ISO moments', () => {
    expect(diffHours('2024-05-10T06:00:00Z', '2024-05-10T12:00:00Z')).toBe(6)
  })

  it('reports a negative diff when the second moment is earlier', () => {
    expect(diffHours('2024-05-10T12:00:00Z', '2024-05-10T06:00:00Z')).toBe(-6)
  })

  it('adds hours and stays a valid UTC ISO string', () => {
    expect(addHoursIso('2024-05-10T06:00:00Z', 6)).toBe('2024-05-10T12:00:00.000Z')
  })
})
