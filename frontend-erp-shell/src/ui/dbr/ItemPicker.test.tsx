import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { searchSpecificationItems } from '../../services/specification'
import { ItemPicker } from './ItemPicker'

vi.mock('../../services/specification', () => ({ searchSpecificationItems: vi.fn() }))

describe('ItemPicker keyboard accessibility', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(searchSpecificationItems).mockResolvedValue({
      items: [{ item_id: 77, item_code: 'ITEM-77', item_name: 'Корпус', item_article: 'A-77' }],
      meta: { count: 1 },
    })
  })

  it('exposes an accessible combobox and selects an option with the keyboard', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<ItemPicker value={null} onChange={onChange} placeholder="Номенклатура" />)
    const input = screen.getByRole('combobox', { name: 'Номенклатура' })

    await user.click(input)
    await user.type(input, 'ко')
    expect(await screen.findByRole('listbox', { name: 'Результаты поиска номенклатуры' })).toBeInTheDocument()
    expect(await screen.findByRole('option', { name: /Корпус/ })).toBeInTheDocument()
    await user.keyboard('{ArrowDown}')
    await user.keyboard('{Enter}')
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ item_id: 77 }))
  })

  it('closes results with Escape and restores focus to the combobox', async () => {
    const user = userEvent.setup()
    render(<ItemPicker value={null} onChange={vi.fn()} placeholder="Номенклатура" />)
    const input = screen.getByRole('combobox', { name: 'Номенклатура' })
    await user.click(input)
    await user.type(input, 'ко')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(input).toHaveFocus()
  })
})
