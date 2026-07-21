import type { Dispatch, SetStateAction } from 'react'
import type { EmployeeOption, ProductionOperationOption } from '../../../domain/productionControl'
import type { PaintWeldChainResult, PaintWeldChainSide } from '../../../services/productionControl'

type Props = {
  chainSaving: boolean
  setChainOpen: Dispatch<SetStateAction<boolean>>
  chainError: string
  chainLoading: boolean
  chainPreview: PaintWeldChainResult | null
  chainWeldOps: ProductionOperationOption[]
  chainPaintOps: ProductionOperationOption[]
  chainOperationEmployees: Record<number, string>
  setChainOperationEmployees: Dispatch<SetStateAction<Record<number, string>>>
  employees: EmployeeOption[]
  employeesLoading: boolean
  submitChainClose: () => void | Promise<void>
}

export function ChainCloseDialog({
  chainSaving,
  setChainOpen,
  chainError,
  chainLoading,
  chainPreview,
  chainWeldOps,
  chainPaintOps,
  chainOperationEmployees,
  setChainOperationEmployees,
  employees,
  employeesLoading,
  submitChainClose,
}: Props) {
  return (
    <div className="dialogOverlay" role="dialog" aria-modal="true" aria-labelledby="chain-close-dialog-title" onClick={(e) => { if (e.target === e.currentTarget && !chainSaving) setChainOpen(false) }}>
      <div className="dialogBox">
        <div className="dialogHeader" id="chain-close-dialog-title">Закрыть цепочку окраска↔сварка</div>
        <div className="dialogBody">
          {chainError && <div className="dialogError" role="alert">{chainError}</div>}
          {chainLoading && <div className="fieldHint" role="status">Загрузка предпросмотра...</div>}
          {chainPreview && (
            <>
              <div className="fieldHint">
                Будут созданы выпуски по обеим строкам, СборкаЗапасов обоих заказов и один
                комбинированный сдельный наряд (основание — окрасочная сборка). Оба заказа
                будут завершены в 1С.
              </div>
              {([
                ['Сварка', chainPreview.weld, chainWeldOps],
                ['Окраска', chainPreview.paint, chainPaintOps],
              ] as Array<[string, PaintWeldChainSide | undefined, ProductionOperationOption[]]>).map(([label, side, ops]) => (
                <div className="dialogField" key={label}>
                  <label>
                    {label}: {Number(side?.qty_to_produce ?? 0) > 0
                      ? `выпуск ${Number(side?.qty_to_produce).toLocaleString('ru-RU')} шт`
                      : `выпуск уже создан (№${side?.existing_manufacture_id ?? '—'})`}
                  </label>
                  {ops.length > 0 && (
                    <div className="operationExecutorList">
                      {ops.map((operation) => (
                        <div className="operationExecutorRow" key={operation.spec_operation_id}>
                          <div className="operationExecutorMeta">
                            <strong>{operation.line_number}. {operation.operation_name || 'Операция'}</strong>
                            <span>{operation.stage_name || 'Этап не указан'} · норма {Number(operation.time_norm ?? 0).toLocaleString('ru-RU')}</span>
                          </div>
                          <select
                            value={chainOperationEmployees[operation.spec_operation_id] || ''}
                            onChange={(e) => setChainOperationEmployees((current) => ({
                              ...current,
                              [operation.spec_operation_id]: e.target.value,
                            }))}
                            disabled={chainSaving || employeesLoading}
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
                  )}
                </div>
              ))}
            </>
          )}
        </div>
        <div className="dialogFooter">
          <button onClick={() => setChainOpen(false)} disabled={chainSaving}>Отмена</button>
          <button
            className="primary"
            onClick={() => void submitChainClose()}
            disabled={
              chainSaving
              || chainLoading
              || !chainPreview
              || (employees.length > 0
                && [...chainWeldOps, ...chainPaintOps].some(
                  (operation) => !chainOperationEmployees[operation.spec_operation_id],
                ))
            }
          >
            {chainSaving ? 'Закрываем...' : 'Закрыть оба заказа в 1С'}
          </button>
        </div>
      </div>
    </div>
  )
}
