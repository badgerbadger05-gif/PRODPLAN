export type RootProductOption = {
  item_id: number
  item_name: string
  item_article?: string | null
  item_code?: string | null
}

export function rootProductOptionLabel(option?: RootProductOption | null) {
  if (!option) return 'Все изделия плана'
  return option.item_article
    ? `${option.item_name} · ${option.item_article}`
    : option.item_name || option.item_code || `Номенклатура #${option.item_id}`
}

export function rootProductLabel(options: RootProductOption[], value: number | null) {
  if (!options.length) return 'Нет строк плана'
  return rootProductOptionLabel(options.find((option) => option.item_id === value) ?? null)
}

