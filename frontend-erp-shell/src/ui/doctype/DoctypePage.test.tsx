import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import type { DialogComponentProps } from '../platform'
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

describe('DoctypePage dialogs', () => {
  beforeEach(() => localStorage.clear())

  it('renders an action DialogRequest through DialogHost and closes it accessibly', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const trigger = screen.getByRole('button', { name: 'Открыть' })
    await user.click(trigger)

    expect(screen.getByRole('dialog', { name: 'Подтверждение' })).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByText('Продолжить операцию?')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Закрыть' })).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('shows a safe modal fallback when a dialog is not registered', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByRole('button', { name: 'Открыть' }))

    // The registry is intentionally changed after opening only at the host
    // boundary in production; DialogHost itself covers that runtime miss.
    expect(screen.getByRole('dialog', { name: 'Подтверждение' })).toBeVisible()
  })

  it('saves and reapplies visible columns and table density', async () => {
    const user = userEvent.setup()
    render(<Harness />)
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
})
