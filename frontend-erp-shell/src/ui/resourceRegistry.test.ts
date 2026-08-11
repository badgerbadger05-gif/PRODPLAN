import { describe, expect, it } from 'vitest'

import { frontendResources } from './resourceRegistry'

describe('frontend route manifest', () => {
  it('does not publish removed DBR or planning-comparison routes', () => {
    const paths = frontendResources.map((resource) => resource.to)

    expect(paths).not.toContain('/planning-comparison')
    expect(paths.some((path) => path === '/dbr' || path.startsWith('/dbr/'))).toBe(false)
  })

  it('keeps navigation shortcuts contiguous after route removal', () => {
    expect(
      frontendResources
        .map((resource) => resource.shortcut)
        .filter(Boolean),
    ).toEqual([
      'Alt+1',
      'Alt+2',
      'Alt+3',
      'Alt+4',
      'Alt+5',
      'Alt+6',
      'Alt+7',
      'Alt+8',
    ])
  })
})
