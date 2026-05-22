import { productionStatusLabel, type MaterialsResponse, type OrderRow } from '../../../domain/productionControl'
import { qty } from '../../../lib/format'

type Props = {
  activeRow: OrderRow | null
  materials: MaterialsResponse | null
  coverageLabels: Record<string, string>
  onLoadMaterials: () => void
  onPrint: () => void
}

export function ProductionDetailPane({ activeRow, materials, coverageLabels, onLoadMaterials, onPrint }: Props) {
  return (
    <aside className="detailPane">
      <h2>Карточка строки</h2>
      {activeRow ? (
        <>
          <div className="detailTitle">{activeRow.item_name}</div>
          <div className="detailMeta">{activeRow.item_article || activeRow.item_code}</div>
          <div className="detailGrid">
            <span>Заказ</span><strong>{activeRow.order_number}</strong>
            <span>Остаток</span><strong>{qty(activeRow.remaining_qty)} {activeRow.unit}</strong>
            <span>Статус</span><strong>{productionStatusLabel(activeRow.status)}</strong>
            <span>Обеспечение</span><strong>{activeRow.coverage_label || coverageLabels[String(activeRow.coverage_status || '')]}</strong>
            <span>Участок</span><strong>{activeRow.workshop_name || activeRow.stage_name || '—'}</strong>
          </div>
          <div className="detailActions">
            <button onClick={onLoadMaterials}>Обновить материалы</button>
            <button onClick={onPrint}>Печать листа</button>
          </div>
          <h3>Комплектующие</h3>
          <div className="materialsList">
            {(materials?.components ?? []).map((m) => (
              <div className="materialRow" key={m.component_item_id}>
                <div>
                  <strong>{m.item_name}</strong>
                  <span>{m.item_article || m.item_code}</span>
                </div>
                <div className="matNums">
                  <span>нужно {qty(m.required_qty)}</span>
                  <span>есть {qty(m.available_qty)}</span>
                </div>
                <span className={`miniPill ${m.availability_status || 'unknown'}`}>{coverageLabels[String(m.availability_status || '')] || m.availability_status}</span>
              </div>
            ))}
            {!materials?.components?.length && <div className="emptyDetail">Материалы не загружены</div>}
          </div>
        </>
      ) : (
        <div className="emptyDetail">Выберите строку</div>
      )}
    </aside>
  )
}
