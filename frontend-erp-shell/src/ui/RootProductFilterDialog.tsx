import { rootProductOptionLabel, type RootProductOption } from './rootProductOptions'

type Props = {
  open: boolean
  title?: string
  options: RootProductOption[]
  value: number | null
  onApply: (value: number | null) => void
  onClose: () => void
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
                <option key={option.item_id} value={option.item_id}>{rootProductOptionLabel(option)}</option>
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
