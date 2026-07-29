import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Button } from './Button'
import { StatusBadge } from './StatusBadge'

describe('minimal UI kit', () => {
  it('keeps native button semantics, props, and legacy variant classes', async () => {
    const onClick = vi.fn()
    render(
      <Button variant="primary" className="extra" disabled onClick={onClick}>
        Выполнить
      </Button>,
    )

    const button = screen.getByRole('button', { name: 'Выполнить' })
    expect(button).not.toHaveAttribute('type')
    expect(button).toHaveClass('primary', 'extra')
    expect(button).toBeDisabled()
    button.click()
    expect(onClick).not.toHaveBeenCalled()
  })

  it('keeps the established pill and miniPill DOM classes', () => {
    const { rerender } = render(<StatusBadge tone="ready">Готово</StatusBadge>)
    expect(screen.getByText('Готово')).toHaveClass('pill', 'ready')

    rerender(<StatusBadge size="small" tone="shortage" className="extra">Дефицит</StatusBadge>)
    expect(screen.getByText('Дефицит')).toHaveClass('miniPill', 'shortage', 'extra')
  })
})
