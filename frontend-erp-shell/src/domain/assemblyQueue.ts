import type { TruthMeta } from './truth'

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

export type AssemblyQueueResponse = {
  rows: AssemblyQueueRow[]
  // Whole-queue totals; `rows` is the requested window of the same queue.
  total_rows: number
  total_queue_qty: number
  limit: number
  offset: number
  truth_meta: TruthMeta
}
