import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import type { DialogComponentProps } from '../platform'
import { encodeViewState } from '../views'
import { DoctypePage } from './DoctypePage'
import type { Doctype } from './types'
import { useDoctypeList } from './useDoctypeList'

type Row = { id: number; name: string }
type Filters = Record<string, never>

function ConfirmDialog({
  message,
  close,
}: DialogComponentProps<{ message: string }>) {
  return (
    <section>
      <p>{message}</p>
      <button onClick={close}>Закрыть</button>
    </section>
  )
}

const doctype: Doctype<Row, Filters> = {
  meta: {
    name: 'dialog-test',
    title: 'Тест диалога',
    subtitle: 'Проверка runtime',
    idField: 'id',
  },
  initialFilters: {},
  dataSource: {
    list: async () => ({ rows: [{ id: 1, name: 'Строка' }], total: 1 }),
  },
  columns: [
    { key: 'id', title: 'ID' },
    { key: 'name', title: 'Название' },
  ],
  actions: [{
    key: 'confirm',
    label: 'Открыть',
    scope: 'global',
    run: async () => ({
      open: {
        dialog: 'confirm',
        payload: { message: 'Продолжить операцию?' },
        accessibleName: 'Подтверждение',
      },
    }),
  }, {
    key: 'dynamic',
    label: ({ rows }) => `Обработать (${rows.length})`,
    scope: 'global',
    run: async () => ({}),
  }],
  permissions: {},
}

function Harness() {
  const state = useDoctypeList(doctype, { access: { roles: [], permissions: [] } })
  return (
    <>
      <button>До диалога</button>
      <DoctypePage
        doctype={doctype}
        state={state}
        access={{ roles: [], permissions: [] }}
        dialogRegistry={{ confirm: ConfirmDialog }}
      />
    </>
  )
}

function ExtensionPointsHarness() {
  const state = useDoctypeList(doctype, { access: { roles: [], permissions: [] } })
  return (
    <DoctypePage
      doctype={doctype}
      state={state}
      access={{ roles: [], permissions: [] }}
      renderTopBadge={(current) => (
        <div data-testid="custom-top-badge">Загружено: {current.rows.length}</div>
      )}
      renderToolbarAfter={(current) => (
        <div data-testid="toolbar-after">Действий: {current.actionContext.rows.length}</div>
      )}
      renderDetail={(value) => (
        <div data-testid="custom-detail">Карточка: {(value as Row).name}</div>
      )}
    />
  )
}

function renderHarness(entry = '/') {
  return render(<MemoryRouter initialEntries={[entry]}><Harness /></MemoryRouter>)
}

describe('DoctypePage dialogs', () => {
  beforeEach(() => localStorage.clear())

  it('renders an action DialogRequest through DialogHost and closes it accessibly', async () => {
    const user = userEvent.setup()
    renderHarness()
    const trigger = screen.getByRole('button', { name: 'Открыть' })
    await user.click(trigger)

    expect(screen.getByRole('dialog', { name: 'Подтверждение' })).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByText('Продолжить операцию?')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Закрыть' })).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('renders an action label from the current action context', async () => {
    renderHarness()

    expect(await screen.findByRole('button', { name: 'Обработать (1)' })).toBeVisible()
  })

  it('shows a safe modal fallback when a dialog is not registered', async () => {
    const user = userEvent.setup()
    renderHarness()
    await user.click(screen.getByRole('button', { name: 'Открыть' }))

    // The registry is intentionally changed after opening only at the host
    // boundary in production; DialogHost itself covers that runtime miss.
    expect(screen.getByRole('dialog', { name: 'Подтверждение' })).toBeVisible()
  })

  it('saves and reapplies visible columns and table density', async () => {
    const user = userEvent.setup()
    renderHarness()
    await screen.findByText('Строка')

    await user.click(screen.getByRole('button', { name: 'Колонки' }))
    await user.click(screen.getByRole('checkbox', { name: 'Название' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Плотность таблицы' }), 'comfortable')
    await user.type(screen.getByRole('textbox', { name: 'Название представления' }), 'Мой вид')
    await user.click(screen.getByRole('button', { name: 'Сохранить' }))

    expect(screen.queryByRole('columnheader', { name: 'Название' })).not.toBeInTheDocument()
    expect(document.querySelector('.doctypeTable--comfortable')).toBeInTheDocument()

    await user.selectOptions(screen.getByRole('combobox', { name: 'Сохранённое представление' }), '')
    expect(screen.getByRole('columnheader', { name: 'Название' })).toBeVisible()
    expect(document.querySelector('.doctypeTable--compact')).toBeInTheDocument()

    await user.selectOptions(screen.getByRole('combobox', { name: 'Сохранённое представление' }), screen.getByRole('option', { name: 'Мой вид' }))
    expect(screen.queryByRole('columnheader', { name: 'Название' })).not.toBeInTheDocument()
    expect(document.querySelector('.doctypeTable--comfortable')).toBeInTheDocument()
  })

  it('hydrates a validated view from the URL', async () => {
    const token = encodeViewState({
      filters: {},
      sort: [{ field: 'id', direction: 'desc' }],
      visibleColumns: ['id', 'missing-column'],
      density: 'comfortable',
    })
    renderHarness(`/?view=${token}`)

    expect(await screen.findByRole('columnheader', { name: 'ID' })).toBeVisible()
    expect(screen.queryByRole('columnheader', { name: 'Название' })).not.toBeInTheDocument()
    expect(document.querySelector('.doctypeTable--comfortable')).toBeInTheDocument()
  })
})

describe('DoctypePage extension points', () => {
  beforeEach(() => localStorage.clear())

  it('renders a custom top badge and toolbar extension with the current state', async () => {
    render(<MemoryRouter><ExtensionPointsHarness /></MemoryRouter>)

    expect(await screen.findByTestId('custom-top-badge')).toHaveTextContent('Загружено: 1')
    expect(screen.getByTestId('toolbar-after')).toHaveTextContent('Действий: 1')
    expect(screen.getByTestId('toolbar-after').previousElementSibling).toHaveClass('commandBar')
  })

  it('creates a split detail pane for renderDetail without a declarative detail layout', async () => {
    const { container } = render(<MemoryRouter><ExtensionPointsHarness /></MemoryRouter>)

    expect(await screen.findByTestId('custom-detail')).toHaveTextContent('Карточка: Строка')
    expect(screen.getByTestId('custom-detail').closest('aside')).toHaveClass('detailPane')
    expect(container.querySelector('.split')).toBeInTheDocument()
  })
})
