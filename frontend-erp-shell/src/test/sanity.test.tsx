import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

describe('vitest runner sanity', () => {
  it('renders a DOM node and matches jest-dom', () => {
    render(<div data-testid="ok">hello</div>)
    expect(screen.getByTestId('ok')).toBeInTheDocument()
    expect(screen.getByTestId('ok')).toHaveTextContent('hello')
  })
})
