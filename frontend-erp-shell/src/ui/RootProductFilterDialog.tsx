export type RootProductOption = {
  item_id: number
  item_name: string
  item_article?: string | null
  item_code?: string | null
}

type Props = {
  open: boolean
  title?: string
  options: RootProductOption[]
  value: number | null
  onApply: (value: number | null) => void
  onClose: () => void
}

function optionLabel(option?: RootProductOption | null) {
  if (!option) return 'Все изделия плана'
  return option.item_article
    ? `${option.item_name} · ${option.item_article}`
    : option.item_name || option.item_code || `Номенклатура #${option.item_id}`
}

export function rootProductLabel(options: RootProductOption[], value: number | null) {
  if (!options.length) return 'Нет строк плана'
  return optionLabel(options.find((option) => option.item_id === value) ?? null)
}

export function RootProductFilterDialog({ open, title = 'Корневое изделие плана', options, value, onApply, onClose }: Props) {
  if (!open) return null
  return (
    <div className="dialogOverlay" onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="dialogBox">
        <div className="dialogHeader">{title}</div>
        <div className="dialogBody">
          <div className="dialogField">
            <label>Строка плана</label>
            <select value={value ?? ''} onChange={(e) => onApply(e.target.value ? Number(e.target.value) : null)}>
              <option value="">Все изделия плана</option>
              {options.map((option) => (
                <option key={option.item_id} value={option.item_id}>{optionLabel(option)}</option>
              ))}
            </select>
          </div>
          {!options.length && <div className="fieldHint">Связанный периодический план не найден или в нём нет строк для отбора.</div>}
        </div>
        <div className="dialogFooter">
          <button onClick={() => onApply(null)}>Сбросить</button>
          <button className="primary" onClick={onClose}>Закрыть</button>
        </div>
      </div>
    </div>
  )
}
