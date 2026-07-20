import type { Dispatch, SetStateAction } from 'react'
import type { EmployeeOption, OrderRow, ProductionOperationOption } from '../../../domain/productionControl'

type Props = {
  produceRow: OrderRow
  produceError: string
  canProduceRow: boolean
  produceQty: string
  setProduceQty: Dispatch<SetStateAction<string>>
  produceSaving: boolean
  produceOverageQty: number
  produceOperations: ProductionOperationOption[]
  produceOperationEmployees: Record<number, string>
  setProduceOperationEmployees: Dispatch<SetStateAction<Record<number, string>>>
  employees: EmployeeOption[]
  employeesLoading: boolean
  produceOperationsLoading: boolean
  produceEmployeeRef: string
  setProduceEmployeeRef: Dispatch<SetStateAction<string>>
  produceDryRun: boolean
  setProduceDryRun: Dispatch<SetStateAction<boolean>>
  setProduceDryRunPayload: Dispatch<SetStateAction<string | null>>
  produceDryRunPayload: string | null
  allOperationExecutorsSelected: boolean
  setProduceOpen: Dispatch<SetStateAction<boolean>>
  submitProduce: () => void | Promise<void>
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
  produceOperationEmployees,
  setProduceOperationEmployees,
  employees,
  employeesLoading,
  produceOperationsLoading,
  produceEmployeeRef,
  setProduceEmployeeRef,
  produceDryRun,
  setProduceDryRun,
  setProduceDryRunPayload,
  produceDryRunPayload,
  allOperationExecutorsSelected,
  setProduceOpen,
  submitProduce,
}: Props) {
  return (
    <div className="dialogOverlay" onClick={(e) => { if (e.target === e.currentTarget) setProduceOpen(false) }}>
      <div className="dialogBox">
        <div className="dialogHeader">Произвести - {produceRow.item_name}</div>
        <div className="dialogBody">
          {produceError && <div className="dialogError">{produceError}</div>}
          {!canProduceRow && (
            <div className="fieldHint danger">Эта строка уже произведена полностью.</div>
          )}
          <div className="dialogField">
            <label>Количество ({produceRow.unit || 'шт'})</label>
            <input
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
          {produceOperations.length > 0 ? (
            <div className="dialogField">
              <label>Исполнители операций</label>
              <div className="operationExecutorList">
                {produceOperations.map((operation) => (
                  <div className="operationExecutorRow" key={operation.spec_operation_id}>
                    <div className="operationExecutorMeta">
                      <strong>{operation.line_number}. {operation.operation_name || 'Операция'}</strong>
                      <span>{operation.stage_name || 'Этап не указан'} · норма {Number(operation.time_norm ?? 0).toLocaleString('ru-RU')}</span>
                    </div>
                    <select
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
                          {employee.employee_name}{employee.employee_type === 'brigade' ? ' [бригада]' : ''}{employee.employee_code ? ` (${employee.employee_code})` : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>
              {!employeesLoading && employees.length === 0 && (
                <div className="fieldHint">Список пуст. Запустите синхронизацию сотрудников в разделе «Синхронизация».</div>
              )}
            </div>
          ) : (
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
                    {employee.employee_name}{employee.employee_type === 'brigade' ? ' [бригада]' : ''}{employee.employee_code ? ` (${employee.employee_code})` : ''}
                  </option>
                ))}
              </select>
              {!employeesLoading && employees.length === 0 && (
                <div className="fieldHint">Список пуст. Запустите синхронизацию сотрудников в разделе «Синхронизация».</div>
              )}
            </div>
          )}
          <div className="dialogCheckRow">
            <input
              type="checkbox"
              id="produceDryRun"
              checked={produceDryRun}
              onChange={(e) => { setProduceDryRun(e.target.checked); setProduceDryRunPayload(null) }}
              disabled={produceSaving}
            />
            <label htmlFor="produceDryRun">dry_run - показать payload, не отправлять в 1С</label>
          </div>
          {produceDryRunPayload && <div className="dialogPreview">{produceDryRunPayload}</div>}
        </div>
        <div className="dialogFooter">
          <button onClick={() => setProduceOpen(false)} disabled={produceSaving}>Отмена</button>
          <button
            className="primary"
            onClick={() => void submitProduce()}
            disabled={!canProduceRow || produceSaving || employeesLoading || produceOperationsLoading || (employees.length > 0 && (produceOperations.length ? !allOperationExecutorsSelected : !produceEmployeeRef))}
          >
            {produceSaving ? 'Создаём...' : produceDryRun ? 'Показать payload' : 'Создать в 1С'}
          </button>
        </div>
      </div>
    </div>
  )
}
