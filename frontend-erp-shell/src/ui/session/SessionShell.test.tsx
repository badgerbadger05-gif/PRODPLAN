import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { App } from '../App'
import { createMockSessionProvider, mockUser, SessionRoot } from './index'

function renderShell(role: Parameters<typeof mockUser>[0], path = '/') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SessionRoot provider={createMockSessionProvider(mockUser(role))}>
        <App />
      </SessionRoot>
    </MemoryRouter>,
  )
}

describe('session-aware ERP shell', () => {
  it('filters navigation and shortcuts from the same viewer grants', async () => {
    renderShell('viewer')

    expect(await screen.findByText('Демо · viewer')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Главная/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Ledger/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Синхронизация/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Журнал закупок/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Синхронизация/ })).not.toBeInTheDocument()
  })

  it('denies a direct route that is absent from the current grants', async () => {
    renderShell('viewer', '/sync')

    expect(await screen.findByRole('alert')).toHaveTextContent('Нет доступа к разделу')
    expect(screen.queryByText('Синхронизация данных')).not.toBeInTheDocument()
  })

  it('shows login after the mock session logs out', async () => {
    renderShell('admin')
    const logout = await screen.findByRole('button', { name: 'Выйти' })
    logout.click()

    await waitFor(() => expect(screen.getByRole('heading', { name: 'PRODPLAN' })).toBeInTheDocument())
    expect(screen.getByText(/Mock-режим/)).toBeInTheDocument()
  })
})
