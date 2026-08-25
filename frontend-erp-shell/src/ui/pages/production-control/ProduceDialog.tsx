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
  produceRow: OrderRow
  produceError: string
  canProduceRow: boolean
  produceQty: string
  setProduceQty: Dispatch<SetStateAction<string>>
  produceSaving: boolean
  produceOverageQty: number
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
  produceRow,
  produceError,
  canProduceRow,
  produceQty,
  setProduceQty,
  produceSaving,
  produceOverageQty,
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
  const needsHeaderExecutor = produceOperations.length === 0
    || chainSides.some((side) => side.operations.length === 0)
  const executorsIncomplete = employees.length > 0 && (
    (produceOperations.length > 0 && !allOperationExecutorsSelected)
    || (needsHeaderExecutor && !produceEmployeeRef)
  )

  function operationList(operations: ProductionOperationOption[], sideKey: string) {
    return (
      <div className="operationExecutorList">
        {operations.map((operation) => (
          <div className="operationExecutorRow" key={`${sideKey}-${operation.spec_operation_id}`}>
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
              disabled={produceSaving || employeesLoading || produceOperationsLoading}
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
          {isChain ? 'Произвести цепочку' : 'Произвести'} - {produceRow.item_name}
        </div>
        <div className="dialogBody">
          {produceError && <div className="dialogError" role="alert">{produceError}</div>}
          {!canProduceRow && (
            <div className="fieldHint danger">Эта строка уже произведена полностью.</div>
          )}
          {isChain ? (
            // Количества сторон цепочки задаёт бэкенд по остатку каждой стороны:
            // одно поле на два заказа означало бы вторую формулу количества.
            <div className="dialogField">
              <label>Цепочка «сварка → окраска»</label>
              <div className="fieldHint">
                Один комбинированный сдельный наряд на обе стороны. Количества берутся
                по остатку каждой стороны.
              </div>
            </div>
          ) : (
            <div className="dialogField">
              <label>Количество ({produceRow.unit || 'шт'})</label>
              <input
                aria-label={`Количество (${produceRow.unit || 'шт'})`}
                type="number"
                min={0}
                step={1}
                value={produceQty}
                onChange={(e) => setProduceQty(e.target.value)}
                disabled={produceSaving}
              />
              {produceOverageQty > 0.000001 && (
                <div className="fieldHint">
                  Больше плана на {produceOverageQty.toLocaleString('ru-RU')}: будет создано дополнительное перемещение материалов.
                </div>
              )}
            </div>
          )}
          {isChain && chainSides.map((side) => (
            <div className="dialogField" key={side.key}>
              <label>{side.title}{side.itemName ? ` — ${side.itemName}` : ''}</label>
              <div className="fieldHint">
                Остаток {Number(side.qty ?? 0).toLocaleString('ru-RU')} {side.unit || 'шт'}
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
            onClick={() => void submitProduce()}
            disabled={!canProduceRow || produceSaving || employeesLoading || produceOperationsLoading || executorsIncomplete}
          >
            {produceSaving ? 'Создаём...' : 'Создать в 1С'}
          </button>
        </div>
      </div>
    </div>
  )
}
