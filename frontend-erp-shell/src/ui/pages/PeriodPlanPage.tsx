import { useNavigate, useParams } from 'react-router-dom'
import { PeriodPlanListView } from './period-plan/PeriodPlanListView'
import { PeriodPlanDetailView } from './period-plan/PeriodPlanDetailView'

// ── Main page (list ↔ detail) ────────────────────────────────────────────────

export function PeriodPlanPage() {
  const { planId: planIdParam } = useParams<{ planId: string }>()
  const navigate = useNavigate()
  const planId = planIdParam ? Number(planIdParam) : null

  if (planId !== null) {
    return (
      <PeriodPlanDetailView
        planId={planId}
        onBack={() => navigate('/period-plan')}
      />
    )
  }

  return (
    <PeriodPlanListView
      onOpenPlan={(id) => navigate(`/period-plan/${id}`)}
    />
  )
}
