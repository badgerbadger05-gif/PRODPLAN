import { describe, expect, it } from 'vitest'
import {
  availableKinds,
  emptyResourceForm,
  normalizeResourcePayload,
  productionKindName,
  resourceToForm,
} from './resourceForm'

describe('resource form helpers', () => {
  it('creates an independent form with production defaults', () => {
    const first = emptyResourceForm()
    const second = emptyResourceForm()

    expect(first).toEqual({
      resource_name: '',
      shift_offset: 0,
      planning_range: 30,
      capacity: 0,
      work_schedule: '5/2',
      daily_work_hours: 8,
      buffer_days: 0,
    })
    expect(first).not.toBe(second)
  })

  it('maps nullable resource values to editable form defaults', () => {
    expect(resourceToForm({
      resource_id: 7,
      resource_name: 'Участок',
      shift_offset: null,
      planning_range: null,
      capacity: null,
      work_schedule: null,
      daily_work_hours: null,
      buffer_days: null,
    })).toEqual({
      resource_name: 'Участок',
      shift_offset: 0,
      planning_range: 30,
      capacity: 0,
      work_schedule: '5/2',
      daily_work_hours: 8,
      buffer_days: 0,
    })
  })

  it('normalizes a resource payload before saving', () => {
    expect(normalizeResourcePayload({
      resource_name: '  Механический участок  ',
      shift_offset: undefined,
      planning_range: undefined,
      capacity: undefined,
      work_schedule: '',
      daily_work_hours: undefined,
      buffer_days: undefined,
    })).toEqual({
      resource_name: 'Механический участок',
      shift_offset: 0,
      planning_range: 30,
      capacity: 0,
      work_schedule: '5/2',
      daily_work_hours: 8,
      buffer_days: 0,
    })
  })

  it('filters assigned and invalid production kinds and sorts the rest', () => {
    const kinds = [
      { id: 3, name: 'Сборка' },
      { id: 2, name: 'Покраска' },
      { id: 1, name: 'Мехобработка' },
      { id: 0, name: 'Некорректный' },
    ]
    const assigned = [{
      id: 10,
      resource_id: 4,
      production_kind_id: 2,
      production_kind_name: 'Покраска',
    }]

    expect(availableKinds(kinds, assigned).map((kind) => kind.id)).toEqual([1, 3])
    expect(productionKindName(kinds, 1)).toBe('Мехобработка')
    expect(productionKindName(kinds, 99)).toBe('ID 99')
  })
})
