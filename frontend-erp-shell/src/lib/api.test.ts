import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, problemMessage, unavailableTruth } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

function respond(status: number, body: unknown) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: false,
    status,
    text: async () => JSON.stringify(body),
  }))
}

describe('problemMessage', () => {
  it('reads the structured truth detail instead of dumping JSON', () => {
    expect(problemMessage({
      code: 'planning_truth_unavailable',
      consumer: 'drum_schedule',
      truth_status: 'stale',
      ledger_generation: 42,
      reason: 'Accepted generation exceeded freshness threshold',
    })).toBe('Accepted generation exceeded freshness threshold (поколение устарело, поколение #42)')
  })

  it('falls back to the code when the reason was overwritten with null', () => {
    expect(problemMessage({
      code: 'drum_schedule_unavailable',
      reason: null,
      truth_status: 'accepted',
      ledger_generation: 7,
    })).toBe('расписание барабана отсутствует для принятого поколения (accepted, поколение #7)')
  })

  it('still prefers the human message of a structured conflict body', () => {
    expect(problemMessage({ message: 'Дефицит компонентов', deficit_lines: [] }))
      .toBe('Дефицит компонентов')
  })

  it('returns null when nothing known is present', () => {
    expect(problemMessage({ deficit_lines: [] })).toBeNull()
    expect(problemMessage([{ loc: ['body'] }])).toBeNull()
  })
})

describe('api error mapping', () => {
  it('throws a readable message for a structured 503', async () => {
    respond(503, {
      detail: {
        code: 'assembly_queue_unavailable',
        reason: 'assembly queue snapshot is missing',
        truth_status: 'uninitialized',
        ledger_generation: null,
      },
    })

    await expect(api('/v1/production-control/assembly-queue')).rejects.toMatchObject({
      status: 503,
      message: 'assembly queue snapshot is missing (поколение Ledger не опубликовано)',
    })
  })

  it('keeps stringifying a detail with no known field', async () => {
    respond(400, { detail: { rows: [1, 2] } })

    await expect(api('/v1/anything')).rejects.toMatchObject({
      message: '{"rows":[1,2]}',
    })
  })
})

describe('unavailableTruth', () => {
  it('recognises a structured 503 as an unavailable truth, not a transport error', () => {
    const error = new ApiError('x', 503, {
      code: 'shelf_projection_unavailable',
      reason: 'shelf projection is missing for accepted generation',
      truth_status: 'accepted',
      ledger_generation: 5,
    })

    expect(unavailableTruth(error)).toEqual({
      reason: 'shelf projection is missing for accepted generation (accepted, поколение #5)',
      code: 'shelf_projection_unavailable',
    })
  })

  it('ignores other statuses and plain errors', () => {
    expect(unavailableTruth(new ApiError('boom', 500, { code: 'x' }))).toBeNull()
    expect(unavailableTruth(new ApiError('boom', 503, 'plain text'))).toBeNull()
    expect(unavailableTruth(new Error('offline'))).toBeNull()
  })
})
