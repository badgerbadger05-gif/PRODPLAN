import type { Dispatch, SetStateAction } from 'react'
import type { WarehouseCandidate } from '../../../domain/productionControl'

type Props = {
  warehousePickerCandidates: WarehouseCandidate[]
  warehousePickerComponents: Array<{ item_name: string; item_article?: string | null; required_qty: number }>
  warehousePickerProductIds: number[]
  warehousePickerSelected: string
  setWarehousePickerOpen: Dispatch<SetStateAction<boolean>>
  setWarehousePickerSelected: Dispatch<SetStateAction<string>>
  confirmWarehousePicker: () => void | Promise<void>
}

export function WarehousePickerDialog({
  warehousePickerCandidates,
  warehousePickerComponents,
  warehousePickerProductIds,
  warehousePickerSelected,
  setWarehousePickerOpen,
  setWarehousePickerSelected,
  confirmWarehousePicker,
}: Props) {
  return (
    <div className="dialogOverlay" onClick={(e) => { if (e.target === e.currentTarget) setWarehousePickerOpen(false) }}>
      <div className="dialogBox">
        <div className="dialogHeader">Выберите склад-источник материалов</div>
        <div className="dialogBody">
          <p>Найдено несколько складов с остатком ({warehousePickerProductIds.length} поз.). Выберите склад отправитель:</p>
          {warehousePickerComponents.length > 0 && (
            <div className="dialogField">
              <label>Детали</label>
              <div className="fieldHint">
                {warehousePickerComponents.map((component, index) => (
                  <div key={`${component.item_name}-${component.item_article ?? ''}-${index}`}>
                    {component.item_name}{component.item_article ? ` (${component.item_article})` : ''} · нужно {component.required_qty.toLocaleString('ru-RU')}
                  </div>
                ))}
              </div>
            </div>
          )}
          {warehousePickerCandidates.map((c) => (
            <div key={c.ref1c} className="dialogField" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                type="radio"
                id={`wh-${c.ref1c}`}
                name="warehousePicker"
                value={c.ref1c}
                checked={warehousePickerSelected === c.ref1c}
                onChange={() => setWarehousePickerSelected(c.ref1c)}
              />
              <label htmlFor={`wh-${c.ref1c}`}>
                {c.name}
                {typeof c.qty === 'number'
                  ? ` (${c.qty.toLocaleString('ru-RU')})`
                  : typeof c.components_covered === 'number' && typeof c.total_components === 'number'
                    ? ` (${c.components_covered}/${c.total_components} компонентов)`
                    : ''}
              </label>
            </div>
          ))}
        </div>
        <div className="dialogFooter">
          <button onClick={() => setWarehousePickerOpen(false)}>Отмена</button>
          <button className="primary" onClick={() => void confirmWarehousePicker()} disabled={!warehousePickerSelected}>
            Подтвердить
          </button>
        </div>
      </div>
    </div>
  )
}
