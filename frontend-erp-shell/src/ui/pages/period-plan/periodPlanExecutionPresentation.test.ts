import { describe, expect, it } from 'vitest'
import { executionWorkItemPresentation } from './periodPlanExecutionPresentation'

describe('period execution presentation', () => {
  it('renders persisted navigation and quantities without domain derivation', () => {
    expect(executionWorkItemPresentation({
      type: 'planned_purchase',
      qty: 5,
      assigned_qty: 0,
      unassigned_qty: 5,
      current_identity: 'mrp-run:41:requirement:101:planned-purchase:701',
      navigation_href: '#/mrp-runs/41?tab=purchases&current_identity=mrp-run%3A41%3Arequirement%3A101%3Aplanned-purchase%3A701',
      navigation_reason: null,
    })).toEqual({
      href: '#/mrp-runs/41?tab=purchases&current_identity=mrp-run%3A41%3Arequirement%3A101%3Aplanned-purchase%3A701',
      assignedQty: 0,
      unassignedQty: 5,
      unavailableReason: null,
    })
  })

  it('fails closed when the backend cannot provide a stable target', () => {
    expect(executionWorkItemPresentation({
      type: 'planned_order',
      qty: 4,
      assigned_qty: null,
      unassigned_qty: null,
      current_identity: null,
      navigation_href: null,
      navigation_reason: 'Точный current target не опубликован',
    })).toEqual({
      href: null,
      assignedQty: null,
      unassignedQty: null,
      unavailableReason: 'Точный current target не опубликован',
    })
  })
})
