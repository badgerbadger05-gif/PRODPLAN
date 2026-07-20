import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  ProductionResource,
  ResourceProductionKind,
  ResourceStage,
} from '../../domain/resources'
import {
  addResourceProductionKind,
  createResource,
  listProductionKinds,
  listResourceProductionKinds,
  listResources,
  listResourceStages,
  removeResourceProductionKind,
  updateResource,
} from '../../services/resources'
import { ResourcesPage } from './ResourcesPage'

vi.mock('../../services/resources', () => ({
  addResourceProductionKind: vi.fn(),
  createResource: vi.fn(),
  listProductionKinds: vi.fn(),
  listResourceProductionKinds: vi.fn(),
  listResources: vi.fn(),
  listResourceStages: vi.fn(),
  removeResourceProductionKind: vi.fn(),
  updateResource: vi.fn(),
}))

const resources: ProductionResource[] = [
  {
    resource_id: 1,
    resource_name: 'Механический участок',
    capacity: 80,
    daily_work_hours: 8,
    work_schedule: '5/2',
    buffer_days: 2,
    shift_offset: 1,
    planning_range: 30,
  },
  {
    resource_id: 2,
    resource_name: 'Сборочный участок',
    capacity: 40,
    daily_work_hours: 12,
    work_schedule: '2/2',
    buffer_days: 1,
    shift_offset: 0,
    planning_range: 21,
  },
]

const stagesByResource: Record<number, ResourceStage[]> = {
  1: [{ id: 101, resource_id: 1, stage_id: 501, stage_name: 'Токарная обработка' }],
  2: [{ id: 102, resource_id: 2, stage_id: 502, stage_name: 'Сборка' }],
}

const kindsByResource: Record<number, ResourceProductionKind[]> = {
  1: [{
    id: 201,
    resource_id: 1,
    production_kind_id: 10,
    production_kind_name: 'Мехобработка',
  }],
  2: [],
}

function resourcesTable() {
  const table = document.querySelector('table.resourcesTable')
  if (!table) throw new Error('resources table not found')
  return within(table as HTMLElement)
}

describe('ResourcesPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listResources).mockResolvedValue(resources)
    vi.mocked(listProductionKinds).mockResolvedValue([
      { id: 10, name: 'Мехобработка' },
      { id: 11, name: 'Покраска' },
    ])
    vi.mocked(listResourceStages).mockImplementation(async (resourceId) => (
      stagesByResource[resourceId] ?? []
    ))
    vi.mocked(listResourceProductionKinds).mockImplementation(async (resourceId) => (
      kindsByResource[resourceId] ?? []
    ))
    vi.mocked(createResource).mockResolvedValue({
      resource_id: 3,
      resource_name: 'Новый участок',
      capacity: 0,
      daily_work_hours: 8,
      work_schedule: '5/2',
      buffer_days: 0,
      shift_offset: 0,
      planning_range: 30,
    })
    vi.mocked(updateResource).mockImplementation(async (resourceId, payload) => ({
      resource_id: resourceId,
      ...payload,
    }))
    vi.mocked(addResourceProductionKind).mockResolvedValue({
      id: 202,
      resource_id: 1,
      production_kind_id: 11,
      production_kind_name: 'Покраска',
    })
    vi.mocked(removeResourceProductionKind).mockResolvedValue({ status: 'ok' })
  })

  it('loads resources and the first selected resource detail', async () => {
    render(<ResourcesPage />)

    expect(await resourcesTable().findByText('Механический участок')).toBeVisible()
    expect(resourcesTable().getByText('Сборочный участок')).toBeVisible()
    expect(screen.getByText('Участков: 2')).toBeVisible()
    expect(await screen.findByText('Токарная обработка')).toBeVisible()
    expect(screen.getByText('Мехобработка')).toBeVisible()
    expect(screen.getByLabelText('Название участка')).toHaveValue('Механический участок')

    expect(listResources).toHaveBeenCalledOnce()
    expect(listProductionKinds).toHaveBeenCalledOnce()
    expect(listResourceStages).toHaveBeenCalledWith(1)
    expect(listResourceProductionKinds).toHaveBeenCalledWith(1)
  })

  it('filters the master table on the client without refetching resources', async () => {
    const user = userEvent.setup()
    render(<ResourcesPage />)
    await resourcesTable().findByText('Механический участок')

    await user.type(screen.getByPlaceholderText('участок'), 'сборочный')

    expect(resourcesTable().queryByText('Механический участок')).not.toBeInTheDocument()
    expect(resourcesTable().getByText('Сборочный участок')).toBeVisible()
    expect(listResources).toHaveBeenCalledOnce()
    // Current behavior keeps the previously selected detail even when its row
    // is hidden by the client-side search.
    expect(screen.getByLabelText('Название участка')).toHaveValue('Механический участок')
  })

  it('supports roving keyboard selection without leaking row shortcuts into the form', async () => {
    const user = userEvent.setup()
    render(<ResourcesPage />)
    await resourcesTable().findByText('Механический участок')
    await waitFor(() => expect(listResourceStages).toHaveBeenCalledWith(1))

    const first = resourcesTable().getByRole('row', { name: /Механический участок/ })
    const second = resourcesTable().getByRole('row', { name: /Сборочный участок/ })
    expect(first).toHaveAttribute('tabindex', '0')
    expect(first).toHaveAttribute('aria-selected', 'true')
    expect(second).toHaveAttribute('tabindex', '-1')
    expect(second).toHaveAttribute('aria-selected', 'false')

    first.focus()
    await user.keyboard('{ArrowDown}')

    expect(second).toHaveFocus()
    expect(first).toHaveAttribute('aria-selected', 'false')
    expect(second).toHaveAttribute('aria-selected', 'true')
    await waitFor(() => expect(screen.getByLabelText('Название участка')).toHaveValue('Сборочный участок'))
    await waitFor(() => expect(listResourceStages).toHaveBeenCalledWith(2))

    vi.mocked(listResourceStages).mockClear()
    vi.mocked(listResourceProductionKinds).mockClear()
    await user.keyboard('{Enter}')
    expect(listResourceStages).not.toHaveBeenCalled()
    expect(listResourceProductionKinds).not.toHaveBeenCalled()

    const nameInput = screen.getByLabelText('Название участка')
    nameInput.focus()
    await user.keyboard('{ArrowUp}')
    expect(nameInput).toHaveFocus()
    expect(second).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('Название участка')).toHaveValue('Сборочный участок')
  })

  it('validates and creates a resource with normalized defaults', async () => {
    const user = userEvent.setup()
    render(<ResourcesPage />)
    await resourcesTable().findByText('Механический участок')

    await user.click(screen.getByRole('button', { name: 'Добавить участок' }))
    await user.click(screen.getByRole('button', { name: 'Сохранить карточку' }))
    expect(screen.getByText('Введите название участка')).toBeVisible()
    expect(createResource).not.toHaveBeenCalled()

    await user.type(screen.getByLabelText('Название участка'), '  Новый участок  ')
    await user.click(screen.getByRole('button', { name: 'Сохранить карточку' }))

    await waitFor(() => expect(createResource).toHaveBeenCalledWith({
      resource_name: 'Новый участок',
      shift_offset: 0,
      planning_range: 30,
      capacity: 0,
      work_schedule: '5/2',
      daily_work_hours: 8,
      buffer_days: 0,
    }))
    expect(await screen.findByText('Участок создан')).toBeVisible()
    expect(listResources).toHaveBeenCalledTimes(2)
  })

  it('updates the selected resource from the editable card', async () => {
    const user = userEvent.setup()
    render(<ResourcesPage />)
    await resourcesTable().findByText('Сборочный участок')

    await user.click(resourcesTable().getByText('Сборочный участок'))
    await waitFor(() => expect(listResourceStages).toHaveBeenCalledWith(2))
    const name = screen.getByLabelText('Название участка')
    await user.clear(name)
    await user.type(name, 'Сборка и упаковка')
    await user.click(screen.getByRole('button', { name: 'Сохранить карточку' }))

    await waitFor(() => expect(updateResource).toHaveBeenCalledWith(2, {
      resource_name: 'Сборка и упаковка',
      shift_offset: 0,
      planning_range: 21,
      capacity: 40,
      work_schedule: '2/2',
      daily_work_hours: 12,
      buffer_days: 1,
    }))
    expect(await screen.findByText('Участок сохранен')).toBeVisible()
  })

  it('adds and removes production-kind bindings and refreshes detail', async () => {
    const user = userEvent.setup()
    render(<ResourcesPage />)
    await screen.findByText('Токарная обработка')

    await user.selectOptions(screen.getByRole('combobox', { name: '' }), '11')
    await user.click(screen.getByRole('button', { name: /^Добавить$/ }))
    await waitFor(() => expect(addResourceProductionKind).toHaveBeenCalledWith(1, 11))
    expect(await screen.findByText('Вид производства привязан к участку')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'x' }))
    await waitFor(() => expect(removeResourceProductionKind).toHaveBeenCalledWith(1, 10))
    expect(await screen.findByText('Привязка вида производства снята')).toBeVisible()
    expect(listResourceProductionKinds).toHaveBeenCalledTimes(3)
  })

  it('shows a service error without rendering stale rows', async () => {
    vi.mocked(listResources).mockRejectedValueOnce(new Error('Ресурсы недоступны'))

    render(<ResourcesPage />)

    expect(await screen.findByText('Ресурсы недоступны')).toBeVisible()
    expect(resourcesTable().queryByText('Механический участок')).not.toBeInTheDocument()
    expect(screen.getByText('Выберите участок')).toBeVisible()
  })
})
