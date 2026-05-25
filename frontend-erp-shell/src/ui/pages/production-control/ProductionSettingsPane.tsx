import type { ControlWarehouse, WorkshopWarehouse } from '../../../domain/productionControl'
import type { ProductionResource } from '../../../domain/resources'

type Props = {
  resources: ProductionResource[]
  warehouses: ControlWarehouse[]
  workshopRows: WorkshopWarehouse[]
  ignoredRefs: Set<string>
  saving: boolean
  onWorkshopRowsChange: (rows: WorkshopWarehouse[]) => void
  onIgnoredRefsChange: (refs: Set<string>) => void
  onSave: () => void
  onClose: () => void
}

export function ProductionSettingsPane({
  resources,
  warehouses,
  workshopRows,
  ignoredRefs,
  saving,
  onWorkshopRowsChange,
  onIgnoredRefsChange,
  onSave,
  onClose,
}: Props) {
  const byResource = new Map(workshopRows.map((row) => [row.resource_id, row.warehouse_ref1c]))

  function setWorkshopWarehouse(resourceId: number, warehouseRef: string) {
    const next = new Map(byResource)
    if (warehouseRef) next.set(resourceId, warehouseRef)
    else next.delete(resourceId)
    onWorkshopRowsChange(Array.from(next.entries()).map(([resource_id, warehouse_ref1c]) => ({ resource_id, warehouse_ref1c })))
  }

  function toggleIgnored(ref: string, checked: boolean) {
    const next = new Set(ignoredRefs)
    if (checked) next.add(ref)
    else next.delete(ref)
    onIgnoredRefsChange(next)
  }

  return (
    <aside className="detailPane settingsPane">
      <div className="paneHeader">
        <div>
          <h2>Настройки журнала</h2>
          <span>Склады выдачи по участкам и склады, которые не участвуют в подборе</span>
        </div>
        <button onClick={onClose}>Закрыть</button>
      </div>

      <div className="settingsBlock settingsWorkshopBlock">
        <h3>Склады получатели по участкам</h3>
        <div className="settingsTableScroll">
          <table className="miniSettingsTable">
            <thead>
              <tr>
                <th>Участок</th>
                <th>Склад получатель</th>
              </tr>
            </thead>
            <tbody>
              {resources.map((resource) => (
                <tr key={resource.resource_id}>
                  <td>{resource.resource_name}</td>
                  <td>
                    <select value={byResource.get(resource.resource_id) ?? ''} onChange={(e) => setWorkshopWarehouse(resource.resource_id, e.target.value)}>
                      <option value="">Не назначен</option>
                      {warehouses.map((warehouse) => (
                        <option key={warehouse.warehouse_ref1c} value={warehouse.warehouse_ref1c}>{warehouseLabel(warehouse)}</option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="settingsBlock settingsIgnoredBlock">
        <h3>Игнорируемые склады</h3>
        <div className="settingsWarehouseList">
          {warehouses.map((warehouse) => (
            <label key={warehouse.warehouse_ref1c} className="selectionRow">
              <input
                type="checkbox"
                checked={ignoredRefs.has(warehouse.warehouse_ref1c)}
                onChange={(e) => toggleIgnored(warehouse.warehouse_ref1c, e.target.checked)}
              />
              <span>{warehouseLabel(warehouse)}</span>
            </label>
          ))}
        </div>
      </div>

      <div className="detailActions">
        <button className="primary" onClick={onSave} disabled={saving}>Сохранить настройки</button>
        <button onClick={onClose} disabled={saving}>Отмена</button>
      </div>
    </aside>
  )
}

function warehouseLabel(warehouse: ControlWarehouse) {
  const code = warehouse.warehouse_code ? `${warehouse.warehouse_code} - ` : ''
  return `${code}${warehouse.warehouse_name || warehouse.warehouse_ref1c}`
}
