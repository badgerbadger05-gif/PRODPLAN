import { describe, expect, it } from 'vitest'
import { canRunAction, canView } from './permissions'

const planner = {
  roles: ['planner'],
  permissions: ['plan.run'],
}

describe('Doctype permissions', () => {
  it('accepts either a role or a granular permission', () => {
    expect(canView({ view: ['planner'] }, planner)).toBe(true)
    expect(canRunAction({ actions: { run: 'plan.run' } }, 'run', planner)).toBe(true)
  })

  it('denies a protected action without its permission', () => {
    expect(canRunAction(
      { actions: { export: 'purchase.export_1c' } },
      'export',
      planner,
    )).toBe(false)
  })

  it('allows an explicit administrative wildcard', () => {
    expect(canRunAction(
      { actions: { export: 'purchase.export_1c' } },
      'export',
      { roles: ['admin'], permissions: ['*'] },
    )).toBe(true)
  })

  it('keeps unspecified gates backwards compatible', () => {
    expect(canView({}, planner)).toBe(true)
    expect(canRunAction({}, 'refresh', planner)).toBe(true)
  })
})
