import { useNavigate } from 'react-router-dom'
import { planningStatusLabel, type PlanningRunRow } from '../../domain/planning'
import { dateTimeRu, qty } from '../../lib/format'
import { DoctypePage, useDoctypeList } from '../doctype'
import type { AccessSubject } from '../doctype/permissions'
import { useOptionalSession } from '../session'
import { mrpPeriodLabel, mrpPlanLabel, mrpRunsDoctype } from './mrpRunsDoctype'

function MrpRunDetail({ run, open }: { run: PlanningRunRow; open: () => void }) {
  return (
    <>
      <h2>Сводка прогона</h2>
      <div className="detailTitle">Прогон #{run.run_id}</div>
      <div className="detailMeta">{planningStatusLabel(run.status)}</div>
      <div className="detailGrid">
        <span>Старт</span><strong>{dateTimeRu(run.started_at) || '—'}</strong>
        <span>Финиш</span><strong>{dateTimeRu(run.finished_at) || '—'}</strong>
        <span>Период</span><strong>{mrpPeriodLabel(run)}</strong>
        <span>План</span><strong>{mrpPlanLabel(run)}</strong>
        <span>Потребность</span><strong>{qty(run.requirement_count)} / {qty(run.requirement_remaining_qty)}</strong>
        <span>Производство</span><strong>{qty(run.order_count)}</strong>
        <span>Закупки</span><strong>{qty(run.purchase_count)}</strong>
        <span>Перегрузы</span><strong>{qty(run.overload_buckets)}</strong>
      </div>
      <div className="detailActions">
        <button className="primary" onClick={open}>Открыть результат</button>
      </div>
    </>
  )
}

export function MrpRunsPage() {
  const navigate = useNavigate()
  const session = useOptionalSession()
  const access: AccessSubject = session?.user ?? { roles: ['viewer'], permissions: [] }
  const state = useDoctypeList(mrpRunsDoctype, { limit: 30, access })
  const openRun = (run: PlanningRunRow) => navigate(`/mrp-runs/${run.run_id}`)

  return (
    <DoctypePage
      doctype={mrpRunsDoctype}
      state={state}
      access={access}
      breadcrumbs="MRP / Прогоны расчёта потребностей"
      onRowDoubleClick={openRun}
      renderDetail={(value) => (
        <MrpRunDetail run={value as PlanningRunRow} open={() => openRun(value as PlanningRunRow)} />
      )}
    />
  )
}
