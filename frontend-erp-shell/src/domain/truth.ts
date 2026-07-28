// Lineage every Ledger-dependent read model carries, mirroring the single
// backend schema `app.routers.truth_meta.TruthMeta`. One shape here too — the
// drum, the shelves and the assembly queue read the same generation.
export type TruthMeta = {
  ledger_generation: number
  cutoff: string
  truth_status: string
  truth_reason: string | null
}
