import type { Dispatch, SetStateAction } from 'react'
import type { EmployeeOption, OrderRow, ProductionOperationOption } from '../../../domain/productionControl'

// Сторона цепочки «сварка → окраска» в диалоге: её операции получают
// исполнителей по тому же правилу, что и операции обычной строки.
export type ProduceChainSide = {
  key: 'weld' | 'paint'
  title: string
  productId: number
  itemName?: string | null
  qty?: number | null
  unit?: string | null
  operations: ProductionOperationOption[]
}

type Props = {
  pieceworkOnly?: boolean
  selectedOperationIds?: number[]
  setSelectedOperationIds?: Dispatch<SetStateAction<number[]>>
  produceRow: OrderRow
  produceError: string
  canProduceRow: boolean
  produceQty: string
  setProduceQty: Dispatch<SetStateAction<string>>
  produceSaving: boolean
  producePartial: boolean
  setProducePartial: Dispatch<SetStateAction<boolean>>
  setProduceChainSides: Dispatch<SetStateAction<ProduceChainSide[] | null>>
  produceOperations: ProductionOperationOption[]
  produceChainSides?: ProduceChainSide[] | null
  produceOperationEmployees: Record<number, string>
  setProduceOperationEmployees: Dispatch<SetStateAction<Record<number, string>>>
  employees: EmployeeOption[]
  employeesLoading: boolean
  produceOperationsLoading: boolean
  produceEmployeeRef: string
  setProduceEmployeeRef: Dispatch<SetStateAction<string>>
  allOperationExecutorsSelected: boolean
  setProduceOpen: Dispatch<SetStateAction<boolean>>
  submitProduce: () => void | Promise<void>
}

function employeeLabel(employee: EmployeeOption) {
  return `${employee.employee_name}${employee.employee_type === 'brigade' ? ' [бригада]' : ''}${employee.employee_code ? ` (${employee.employee_code})` : ''}`
}

export function ProduceDialog({
  pieceworkOnly = false,
  selectedOperationIds = [],
  setSelectedOperationIds,
  produceRow,
  produceError,
  canProduceRow,
  produceQty,
  setProduceQty,
  produceSaving,
  producePartial,
  setProducePartial,
  setProduceChainSides,
  produceOperations,
  produceChainSides,
  produceOperationEmployees,
  setProduceOperationEmployees,
  employees,
  employeesLoading,
  produceOperationsLoading,
  produceEmployeeRef,
  setProduceEmployeeRef,
  allOperationExecutorsSelected,
  setProduceOpen,
  submitProduce,
}: Props) {
  const chainSides = produceChainSides ?? []
  const isChain = chainSides.length > 0
  // Правило одно на оба пути: пока хоть одна операция без исполнителя, в 1С
  // ничего не уходит. Сторона без операций спецификации берёт исполнителя из
  // общего поля — как и обычная строка без операций.
  const needsHeaderExecutor = !pieceworkOnly && (produceOperations.length === 0
    || chainSides.some((side) => side.operations.length === 0))
  const executorsIncomplete = (pieceworkOnly && selectedOperationIds.length === 0) || employees.length > 0 && (
    (produceOperations.length > 0 && !allOperationExecutorsSelected)
    || (needsHeaderExecutor && !produceEmployeeRef)
  )

  function operationList(operations: ProductionOperationOption[], sideKey: string) {
    return (
      <div className="operationExecutorList">
        {operations.map((operation) => (
          <div className="operationExecutorRow" key={`${sideKey}-${operation.spec_operation_id}`}>
            {pieceworkOnly && <input type="checkbox" aria-label={`Оформить операцию ${operation.operation_name}`}
              style={{ width: 'auto' }} checked={selectedOperationIds.includes(operation.spec_operation_id)}
              onChange={(e) => setSelectedOperationIds?.((current) => e.target.checked
                ? [...current, operation.spec_operation_id] : current.filter((id) => id !== operation.spec_operation_id))} />}
            <div className="operationExecutorMeta">
              <strong>{operation.line_number}. {operation.operation_name || 'Операция'}</strong>
              <span>{operation.stage_name || 'Этап не указан'} · норма {Number(operation.time_norm ?? 0).toLocaleString('ru-RU')}</span>
            </div>
            <select
              aria-label={`Исполнитель операции ${operation.line_number}. ${operation.operation_name || 'Операция'}`}
              value={produceOperationEmployees[operation.spec_operation_id] || ''}
              onChange={(e) => setProduceOperationEmployees((current) => ({
                ...current,
                [operation.spec_operation_id]: e.target.value,
              }))}
              disabled={produceSaving || employeesLoading || produceOperationsLoading || (pieceworkOnly && !selectedOperationIds.includes(operation.spec_operation_id))}
            >
              <option value="">{employeesLoading ? 'Загрузка сотрудников...' : 'Выберите сотрудника'}</option>
              {employees.map((employee) => (
                <option key={employee.employee_ref1c} value={employee.employee_ref1c}>
                  {employeeLabel(employee)}
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="dialogOverlay" role="dialog" aria-modal="true" aria-labelledby="produce-dialog-title" onClick={(e) => { if (e.target === e.currentTarget) setProduceOpen(false) }}>
      <div className="dialogBox">
        <div className="dialogHeader" id="produce-dialog-title">
          {pieceworkOnly ? 'Сдельный наряд' : isChain ? 'Произвести цепочку' : 'Произвести'} - {produceRow.item_name}
        </div>
        <div className="dialogBody">
          {!pieceworkOnly && <><div className="fieldHint">Укажите фактическое количество — оно может быть меньше или больше заказа. Выпуск и сдельный наряд будут оформлены на это количество, затем заказ завершится в 1С.</div>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '10px 0' }}>
            <input style={{ width: 'auto', height: 'auto', margin: 0 }} type="checkbox" checked={producePartial} disabled={produceSaving}
              onChange={(e) => setProducePartial(e.target.checked)} />
            Частичный выпуск
          </label>
          <div className="fieldHint">При частичном выпуске заказ остаётся открытым. Следующая операция создаст новые производство и сдельный наряд под тем же заказом. Материалы проверяются перед выпуском.</div></>}
          {pieceworkOnly && <div className="fieldHint">Укажите общее выполненное количество по выбранным операциям этого заказа. Уже оформленное в 1С количество будет исключено. Производство и окраска не оформляются, заказ не закрывается.</div>}
          {produceError && <div className="dialogError" role="alert">{produceError}</div>}
          {!canProduceRow && (
            <div className="fieldHint danger">Эта строка уже произведена полностью.</div>
          )}
          {isChain ? (
            // Каждая сторона сохраняет отдельное фактическое количество.
            <div className="dialogField">
              <label>Цепочка «сварка → окраска»</label>
              <div className="fieldHint">
                Один комбинированный сдельный наряд на обе стороны за этот выпуск.
                Укажите фактическое количество каждой стороны.
              </div>
            </div>
          ) : (
            <div className="dialogField">
              <label>{pieceworkOnly ? 'Всего выполнено по выбранным операциям' : 'Количество'} ({produceRow.unit || 'шт'})</label>
              <input
                aria-label={`Количество (${produceRow.unit || 'шт'})`}
                type="number"
                min={0}
                step={1}
                value={produceQty}
                onChange={(e) => setProduceQty(e.target.value)}
                disabled={produceSaving}
              />

            </div>
          )}
          {isChain && chainSides.map((side) => (
            <div className="dialogField" key={side.key}>
              <label>{side.title}{side.itemName ? ` — ${side.itemName}` : ''}</label>
              <div className="fieldHint">
                <input aria-label={`Количество: ${side.title}`} type="number" min={0} step={1}
                  value={side.qty ?? 0} disabled={produceSaving}
                  onChange={(e) => setProduceChainSides((current) => current?.map((entry) =>
                    entry.key === side.key ? { ...entry, qty: Number(e.target.value) } : entry) ?? null)} />
              </div>
              {side.operations.length > 0 ? operationList(side.operations, side.key) : (
                <div className="fieldHint danger">
                  У этой стороны нет операций спецификации — исполнителя берём из общего поля ниже.
                </div>
              )}
            </div>
          ))}
          {!isChain && produceOperations.length > 0 && (
            <div className="dialogField">
              <label>Исполнители операций</label>
              {operationList(produceOperations, 'row')}
            </div>
          )}
          {produceOperations.length > 0 && !employeesLoading && employees.length === 0 && (
            <div className="fieldHint">Список пуст. Запустите синхронизацию сотрудников в разделе «Синхронизация».</div>
          )}
          {needsHeaderExecutor && (
            <div className="dialogField">
              <label>Исполнитель</label>
              <select
                value={produceEmployeeRef}
                onChange={(e) => setProduceEmployeeRef(e.target.value)}
                disabled={produceSaving || employeesLoading || produceOperationsLoading}
              >
                <option value="">{produceOperationsLoading || employeesLoading ? 'Загрузка...' : 'Выберите сотрудника'}</option>
                {employees.map((employee) => (
                  <option key={employee.employee_ref1c} value={employee.employee_ref1c}>
                    {employeeLabel(employee)}
                  </option>
                ))}
              </select>
              {!employeesLoading && employees.length === 0 && (
                <div className="fieldHint">Список пуст. Запустите синхронизацию сотрудников в разделе «Синхронизация».</div>
              )}
            </div>
          )}
        </div>
        <div className="dialogFooter">
          <button onClick={() => setProduceOpen(false)} disabled={produceSaving}>Отмена</button>
          <button
            className="primary"
            style={pieceworkOnly ? { background: '#ff38a4', borderColor: '#c21874', color: '#111' } : undefined}
            onClick={() => void submitProduce()}
            disabled={!canProduceRow || produceSaving || employeesLoading || produceOperationsLoading || executorsIncomplete}
          >
            {produceSaving ? 'Выполняем...' : pieceworkOnly ? 'Создать сдельный наряд' : 'Произвести'}
          </button>
        </div>
      </div>
    </div>
  )
}
