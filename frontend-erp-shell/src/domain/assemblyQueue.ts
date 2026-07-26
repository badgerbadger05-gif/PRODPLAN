export type AssemblyQueuePriorityPart = string | number

export type AssemblyQueueRow = {
  plan_id: number
  plan_line_id: number
  run_id: number
  item_id: number
  item_code: string
  item_name: string
  bucket_date: string
  period_from: string
  period_to: string
  planned_output_qty: number
  accepted_plan_output_qty: number
  assembly_remaining_qty: number
  priority_key: AssemblyQueuePriorityPart[]
}

export type AssemblyQueueTruthMeta = {
  ledger_generation: number
  cutoff: string
  truth_status: string
  truth_reason: string | null
}

export type AssemblyQueueResponse = {
  rows: AssemblyQueueRow[]
  total_rows: number
  total_queue_qty: number
  truth_meta: AssemblyQueueTruthMeta
}
