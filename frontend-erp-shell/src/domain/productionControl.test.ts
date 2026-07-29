import { describe, expect, it } from 'vitest'
import {
  isShelfPullRow,
  paintWeldChainSidesLabel,
  paintWeldChainStateLabel,
} from './productionControl'

describe('isShelfPullRow', () => {
  it('marks only the launches pulled by a DBR shelf', () => {
    expect(isShelfPullRow({ launch_source: 'shelf_pull' })).toBe(true)
    expect(isShelfPullRow({ launch_source: 'mrp_remaining' })).toBe(false)
  })

  it('treats a row without launch_source as a plain MRP remainder', () => {
    // Старый ответ журнала не несёт поля — бейдж «Полка» показывать нельзя.
    expect(isShelfPullRow({})).toBe(false)
  })
})

describe('paint-weld chain labels', () => {
  it('names every state the close can report', () => {
    expect(paintWeldChainStateLabel('partially_posted')).toBe('Проведена частично')
    expect(paintWeldChainStateLabel('manufactures_posted_piecework_pending')).toBe(
      'Сборки проведены, наряда нет',
    )
    expect(paintWeldChainStateLabel('closed')).toBe('Закрыта')
  })

  it('renders the posted/pending sides in Russian', () => {
    expect(paintWeldChainSidesLabel(['weld', 'paint'])).toBe('сварка, окраска')
    expect(paintWeldChainSidesLabel([])).toBe('')
  })
})
