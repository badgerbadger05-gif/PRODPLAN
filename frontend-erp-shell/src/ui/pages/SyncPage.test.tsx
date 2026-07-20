import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fullSyncOrder, syncActions } from '../../domain/sync'
import { downloadBase64File } from '../../lib/download'
import {
  exportProductionOrdersReport,
  exportSupplierOrdersReport,
  fetchODataMetadata,
  getODataConfig,
  listNomenclatureGroups,
  listWarehouses,
  runSyncAction,
  saveNomenclatureGroupSelection,
  saveODataConfig,
  saveWarehouseSelection,
  testODataConnection,
} from '../../services/sync'
import { SyncPage } from './SyncPage'

vi.mock('../../lib/download', () => ({
  downloadBase64File: vi.fn(),
}))

vi.mock('../../services/sync', () => ({
  exportProductionOrdersReport: vi.fn(),
  exportSupplierOrdersReport: vi.fn(),
  fetchODataMetadata: vi.fn(),
  getODataConfig: vi.fn(),
  listNomenclatureGroups: vi.fn(),
  listWarehouses: vi.fn(),
  runSyncAction: vi.fn(),
  saveNomenclatureGroupSelection: vi.fn(),
  saveODataConfig: vi.fn(),
  saveWarehouseSelection: vi.fn(),
  testODataConnection: vi.fn(),
}))

const config = {
  base_url: 'https://1c.example.test/odata',
  username: 'sync-user',
  password: 'secret',
  token: 'token',
}

const warehouses = [
  {
    warehouse_id: 1,
    warehouse_ref1c: 'warehouse-main',
    warehouse_code: 'ОСН',
    warehouse_name: 'Основной склад',
    is_selected: true,
  },
  {
    warehouse_id: 2,
    warehouse_ref1c: 'warehouse-parts',
    warehouse_code: 'КМП',
    warehouse_name: 'Комплектующие',
    is_selected: false,
  },
]

const groups = [
  { id: 'group-pumps', code: 'НАС', name: 'Насосы' },
  { id: 'group-parts', code: 'КМП', name: 'Комплектующие' },
]

function panel(title: string) {
  const heading = screen.getByRole('heading', { name: title })
  const element = heading.closest('.syncPanel')
  if (!element) throw new Error(`sync panel not found: ${title}`)
  return within(element as HTMLElement)
}

describe('SyncPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getODataConfig).mockResolvedValue(config)
    vi.mocked(listWarehouses).mockResolvedValue({
      rows: warehouses,
      total: warehouses.length,
      selected_total: 1,
    })
    vi.mocked(listNomenclatureGroups).mockResolvedValue({
      items: groups,
      selected_ids: ['group-pumps'],
    })
    vi.mocked(saveODataConfig).mockResolvedValue({ status: 'ok', config })
    vi.mocked(testODataConnection).mockResolvedValue({ status: 'ok' })
    vi.mocked(fetchODataMetadata).mockResolvedValue({ entities: 42 })
    vi.mocked(runSyncAction).mockResolvedValue({ created: 1, updated: 2 })
    vi.mocked(saveWarehouseSelection).mockResolvedValue({ status: 'ok' })
    vi.mocked(saveNomenclatureGroupSelection).mockResolvedValue({ status: 'ok' })
    vi.mocked(exportProductionOrdersReport).mockResolvedValue({
      data_base64: 'UFJPRFVDVElPTg==',
      filename: 'production.xlsx',
      content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    vi.mocked(exportSupplierOrdersReport).mockResolvedValue({
      data_base64: 'U1VQUExJRVI=',
      filename: 'supplier.xlsx',
      content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
  })

  it('bootstraps connection config, warehouses and nomenclature groups', async () => {
    render(<SyncPage />)

    expect(await screen.findByDisplayValue(config.base_url)).toBeVisible()
    expect(screen.getByDisplayValue(config.username)).toBeVisible()
    expect(panel('Склады для остатков').getByText('Всего: 2 · Выбрано: 1')).toBeVisible()
    expect(panel('Группы номенклатуры').getByText('Всего: 2 · Выбрано: 1')).toBeVisible()
    expect(screen.getByLabelText(/ОСН — Основной склад/)).toBeChecked()
    expect(screen.getByLabelText(/НАС — Насосы/)).toBeChecked()

    expect(getODataConfig).toHaveBeenCalledOnce()
    expect(listWarehouses).toHaveBeenCalledOnce()
    expect(listNomenclatureGroups).toHaveBeenCalledOnce()
  })

  it('saves connection settings and runs connection and metadata diagnostics', async () => {
    const user = userEvent.setup()
    render(<SyncPage />)
    await screen.findByDisplayValue(config.base_url)

    await user.click(screen.getByRole('button', { name: 'Сохранить настройки' }))
    await waitFor(() => expect(saveODataConfig).toHaveBeenCalledWith(config))
    expect(await screen.findByText('Сохранить настройки: выполнено')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Тест подключения' }))
    await waitFor(() => expect(testODataConnection).toHaveBeenCalledWith(config))
    expect(await screen.findByText('Тест подключения: выполнено')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Выгрузить метаданные' }))
    await waitFor(() => expect(fetchODataMetadata).toHaveBeenCalledWith(config))
    expect(await screen.findByText('Выгрузить метаданные: выполнено')).toBeVisible()
  })

  it('runs a single sync command and records its result', async () => {
    const user = userEvent.setup()
    const action = syncActions.find((item) => item.id === 'nomenclature')!
    render(<SyncPage />)
    await screen.findByDisplayValue(config.base_url)

    await user.click(screen.getByRole('button', { name: action.title }))

    await waitFor(() => expect(runSyncAction).toHaveBeenCalledWith(config, action))
    expect(await screen.findByText(`${action.title}: выполнено`)).toBeVisible()
    const log = screen.getByRole('heading', { name: 'Журнал операций' }).parentElement!
    expect(within(log).getByText('ok')).toBeVisible()
    expect(within(log).getByText(/"created":1/)).toBeVisible()
  })

  it.todo('records a failed single sync command without an unhandled promise rejection')

  it('runs the full sync in the declared sequence', async () => {
    const user = userEvent.setup()
    render(<SyncPage />)
    await screen.findByDisplayValue(config.base_url)

    await user.click(screen.getByRole('button', { name: 'Запустить полную синхронизацию' }))
    expect(await screen.findByText('Полная синхронизация завершена')).toBeVisible()

    expect(vi.mocked(runSyncAction).mock.calls.map(([, action]) => action.id)).toEqual(fullSyncOrder)
    expect(screen.getByText(`${fullSyncOrder.length} из ${fullSyncOrder.length} · 100%`)).toBeVisible()
    expect(listWarehouses).toHaveBeenCalledTimes(2)
    expect(listNomenclatureGroups).toHaveBeenCalledTimes(2)
  })

  it('stops the full sync on the first failed command and keeps failure evidence', async () => {
    const user = userEvent.setup()
    vi.mocked(runSyncAction).mockImplementation(async (_config, action) => {
      if (action.id === 'operations') throw new Error('Операции 1С недоступны')
      return { status: 'ok' }
    })
    render(<SyncPage />)
    await screen.findByDisplayValue(config.base_url)

    await user.click(screen.getByRole('button', { name: 'Запустить полную синхронизацию' }))

    expect((await screen.findAllByText('Операции 1С недоступны')).length).toBe(2)
    const expected = fullSyncOrder.slice(0, fullSyncOrder.indexOf('operations') + 1)
    expect(vi.mocked(runSyncAction).mock.calls.map(([, action]) => action.id)).toEqual(expected)
    const log = screen.getByRole('heading', { name: 'Журнал операций' }).parentElement!
    expect(within(log).getByText('Полная синхронизация')).toBeVisible()
    expect(within(log).getByText('error')).toBeVisible()
    expect(listWarehouses).toHaveBeenCalledOnce()
    expect(listNomenclatureGroups).toHaveBeenCalledOnce()
  })

  it('persists warehouse and nomenclature-group selections', async () => {
    const user = userEvent.setup()
    render(<SyncPage />)
    await screen.findByLabelText(/ОСН — Основной склад/)

    await user.click(panel('Склады для остатков').getByLabelText(/КМП — Комплектующие/))
    await user.click(panel('Склады для остатков').getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(saveWarehouseSelection).toHaveBeenCalledWith([
      'warehouse-main',
      'warehouse-parts',
    ]))

    await user.click(panel('Группы номенклатуры').getByLabelText(/НАС — Насосы/))
    await user.click(panel('Группы номенклатуры').getByLabelText(/КМП — Комплектующие/))
    await user.click(panel('Группы номенклатуры').getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(saveNomenclatureGroupSelection).toHaveBeenCalledWith(['group-parts']))
  })

  it('downloads both Excel reports with their fallback filenames', async () => {
    const user = userEvent.setup()
    render(<SyncPage />)
    await screen.findByDisplayValue(config.base_url)

    await user.click(panel('Excel-отчёты').getByRole('button', { name: 'Заказы на производство' }))
    await waitFor(() => expect(downloadBase64File).toHaveBeenCalledWith(
      expect.objectContaining({ data_base64: 'UFJPRFVDVElPTg==' }),
      'production_orders.xlsx',
    ))

    await user.click(screen.getByRole('button', { name: 'Учитываемые заказы поставщику' }))
    await waitFor(() => expect(downloadBase64File).toHaveBeenCalledWith(
      expect.objectContaining({ data_base64: 'U1VQUExJRVI=' }),
      'supplier_orders.xlsx',
    ))
  })
})
