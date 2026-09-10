import { describe, expect, it } from 'vitest'
import {
  executionJournalRowPresentation,
  executionWorkItemPresentation,
} from './periodPlanExecutionPresentation'

describe('period execution presentation', () => {
  it('renders persisted navigation and quantities without domain derivation', () => {
    expect(executionWorkItemPresentation({
      type: 'planned_purchase',
      qty: 5,
      assigned_qty: 0,
      unassigned_qty: 5,
      current_identity: 'mrp-run:41:purchase:requirement:101:item:501:allocation:default',
      navigation_href: '#/mrp-runs/41?tab=purchases&current_identity=mrp-run%3A41%3Apurchase%3Arequirement%3A101%3Aitem%3A501%3Aallocation%3Adefault',
      navigation_reason: null,
    })).toEqual({
      href: '#/mrp-runs/41?tab=purchases&current_identity=mrp-run%3A41%3Apurchase%3Arequirement%3A101%3Aitem%3A501%3Aallocation%3Adefault',
      assignedQty: 0,
      unassignedQty: 5,
      unavailableReason: null,
    })
  })

  it('does not derive status or quantities when accepted DTO fields are unavailable', () => {
    expect(executionJournalRowPresentation({
      status_label: null,
      explanations: ['Current execution is unavailable'],
      current_identity: null,
      source_revision: null,
      unassigned_qty: null,
    })).toEqual({
      statusLabel: null,
      explanations: ['Current execution is unavailable'],
      currentIdentity: null,
      sourceRevision: null,
      unassignedQty: null,
      unavailable: true,
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
