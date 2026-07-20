import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BomItem, SpecNode } from '../../domain/specification'
import { downloadBase64File } from '../../lib/download'
import {
  exportSpecificationXlsx,
  getSpecificationFlattened,
  getSpecificationFull,
  getSpecificationQuality,
  getSpecificationWhereUsed,
  searchSpecificationItems,
} from '../../services/specification'
import { SpecificationPage } from './SpecificationPage'

vi.mock('../../lib/download', () => ({
  downloadBase64File: vi.fn(),
}))

vi.mock('../../services/specification', () => ({
  exportSpecificationXlsx: vi.fn(),
  getSpecificationFlattened: vi.fn(),
  getSpecificationFull: vi.fn(),
  getSpecificationQuality: vi.fn(),
  getSpecificationWhereUsed: vi.fn(),
  searchSpecificationItems: vi.fn(),
}))

const pump: BomItem = {
  item_id: 100,
  item_code: 'PUMP-01',
  item_name: 'Насос ГА-1',
  item_article: 'НАС-01',
  unit: 'шт',
  replenishment_method: 'Производство',
  stock_qty: 2,
  spec_id: 10,
  spec_name: 'СП-10',
  has_children: true,
}

const reducer: BomItem = {
  ...pump,
  item_id: 101,
  item_code: 'GEAR-01',
  item_name: 'Редуктор',
  item_article: 'РЕД-01',
  spec_id: 11,
}

const nodes: SpecNode[] = [{
  id: 'root',
  parentId: null,
  type: 'item',
  name: 'Насос ГА-1',
  article: 'НАС-01',
  unit: 'шт',
  replenishmentMethod: 'Производство',
  computed: { treeQty: 1 },
  children: [
    {
      id: 'bearing',
      parentId: 'root',
      type: 'item',
      name: 'Подшипник',
      article: 'ПД-01',
      stage: { id: 8, name: 'Сборка' },
      replenishmentMethod: 'Закупка',
      qtyPerParent: 2,
      unit: 'шт',
      computed: { treeQty: 2 },
      warnings: ['NO_STOCK'],
    },
    {
      id: 'turning',
      parentId: 'root',
      type: 'operation',
      operation: { id: 4, name: 'Токарная операция' },
      stage: { id: 7, name: 'Мехобработка' },
      timeNormNh: 0.5,
      computed: { treeTimeNh: 0.5 },
    },
  ],
}]

function mockSuccessfulBom() {
  vi.mocked(getSpecificationFull).mockResolvedValue({ nodes, meta: {} })
  vi.mocked(getSpecificationFlattened).mockResolvedValue({
    items: [
      {
        item_id: 200,
        item_code: 'BEARING-01',
        article: 'ПД-01',
        name: 'Подшипник',
        unit: 'шт',
        replenishment_method: 'Закупка',
        total_qty: 2,
        occurrences: 1,
        levels: [1],
        stages: ['Сборка'],
        paths: [{ level: 1, qty: 2, path: 'Насос / Подшипник' }],
        warnings: [],
      },
      {
        item_id: 201,
        item_code: 'SHAFT-01',
        article: 'ВАЛ-01',
        name: 'Вал',
        unit: 'шт',
        replenishment_method: 'Производство',
        total_qty: 1,
        occurrences: 1,
        levels: [1],
        stages: ['Мехобработка'],
        paths: [{ level: 1, qty: 1, path: 'Насос / Вал' }],
        warnings: [],
      },
    ],
    meta: {},
  })
  vi.mocked(getSpecificationWhereUsed).mockResolvedValue({
    items: [{
      parent: reducer,
      spec: { spec_id: 11, spec_name: 'СП-11' },
      component_item_id: 100,
      qty_per_parent: 1,
      total_qty_to_target: 1,
      level_up: 1,
      stage: { id: 8, name: 'Сборка' },
      path: [{ item_id: 101, name: 'Редуктор' }],
    }],
    meta: {},
  })
  vi.mocked(getSpecificationQuality).mockResolvedValue({
    issues: [{
      code: 'NO_STOCK',
      severity: 'warning',
      message: 'Недостаточный остаток',
      item: {
        item_id: 200,
        item_code: 'BEARING-01',
        item_article: 'ПД-01',
        item_name: 'Подшипник',
      },
      spec_id: 10,
    }],
    meta: {},
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <SpecificationPage />
    </MemoryRouter>,
  )
}

async function searchFor(user: ReturnType<typeof userEvent.setup>, query: string) {
  const input = screen.getByPlaceholderText('Артикул, код, название')
  await user.clear(input)
  await user.type(input, query)
  await user.click(screen.getByRole('button', { name: 'Найти' }))
}

describe('SpecificationPage characterization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockSuccessfulBom()
    vi.mocked(exportSpecificationXlsx).mockResolvedValue({
      data_base64: 'WA==',
      filename: 'bom.xlsx',
      content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
  })

  it('shows multiple search matches and lets the user pick one', async () => {
    const user = userEvent.setup()
    vi.mocked(searchSpecificationItems).mockResolvedValue({
      items: [pump, reducer],
      meta: { count: 2 },
    })
    renderPage()

    expect(screen.getByText('Введите артикул, код или часть названия и нажмите Найти')).toBeVisible()
    await searchFor(user, 'нас')

    expect(await screen.findByText('Найдено позиций: 2')).toBeVisible()
    expect(screen.getByText('Насос ГА-1')).toBeVisible()
    expect(screen.getByText('Редуктор')).toBeVisible()
    const pumpRow = screen.getByRole('row', { name: /Насос ГА-1/ })
    await user.click(within(pumpRow).getByRole('button', { name: 'Открыть' }))
    expect(await screen.findByText('Загружено: НАС-01 · Насос ГА-1')).toBeVisible()
  })

  it('auto-loads a single result through all four BOM endpoints in parallel', async () => {
    const user = userEvent.setup()
    vi.mocked(searchSpecificationItems).mockResolvedValue({ items: [pump], meta: { count: 1 } })
    renderPage()

    await searchFor(user, 'НАС-01')
    expect(await screen.findByText('Загружено: НАС-01 · Насос ГА-1')).toBeVisible()

    const expectedRoot = { item_id: 100, root_qty: 1, max_depth: 20 }
    expect(getSpecificationFull).toHaveBeenCalledWith(expectedRoot)
    expect(getSpecificationFlattened).toHaveBeenCalledWith(expectedRoot)
    expect(getSpecificationWhereUsed).toHaveBeenCalledWith({ item_id: 100, max_depth: 10 })
    expect(getSpecificationQuality).toHaveBeenCalledWith({ item_id: 100, max_depth: 20 })
  })

  it('preserves tab, tree and replenishment-method filters and exports XLSX', async () => {
    const user = userEvent.setup()
    vi.mocked(searchSpecificationItems).mockResolvedValue({ items: [pump], meta: { count: 1 } })
    renderPage()
    await searchFor(user, 'НАС-01')
    await screen.findByText('Загружено: НАС-01 · Насос ГА-1')

    await user.type(screen.getByPlaceholderText('Узел, этап, проблема'), 'подшипник')
    expect(screen.getByText('Подшипник')).toBeVisible()
    expect(screen.queryByText('Токарная операция')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Плоская развертка' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Метод' }), 'Закупка')
    expect(screen.getByText('Подшипник')).toBeVisible()
    expect(screen.queryByText('Вал')).not.toBeInTheDocument()

    await user.click(screen.getAllByRole('button', { name: 'Где используется' })[0])
    expect(screen.getByText('СП-11')).toBeVisible()
    await user.click(screen.getAllByRole('button', { name: 'Качество' })[0])
    expect(screen.getByText('Недостаточный остаток')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'XLSX' }))
    await waitFor(() => expect(exportSpecificationXlsx).toHaveBeenCalledWith({
      item_id: 100,
      root_qty: 1,
      max_depth: 20,
      replenishment_method: 'Закупка',
    }))
    expect(downloadBase64File).toHaveBeenCalledWith(
      expect.objectContaining({ filename: 'bom.xlsx' }),
      'specification_НАС-01.xlsx',
    )
  })

  it('shows a BOM load failure and succeeds on retry', async () => {
    const user = userEvent.setup()
    vi.mocked(searchSpecificationItems).mockResolvedValue({ items: [pump], meta: { count: 1 } })
    vi.mocked(getSpecificationFull)
      .mockRejectedValueOnce(new Error('BOM временно недоступен'))
      .mockResolvedValueOnce({ nodes, meta: {} })
    renderPage()

    await searchFor(user, 'НАС-01')
    expect(await screen.findByText('BOM временно недоступен')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Найти' }))
    expect(await screen.findByText('Загружено: НАС-01 · Насос ГА-1')).toBeVisible()
    expect(getSpecificationFull).toHaveBeenCalledTimes(2)
  })
})
