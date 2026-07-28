type Props = {
  reason: string
}

/** «Правда недоступна» — a blocked read, not a broken screen.
 *
 * The Ledger-backed reads answer 503 with a structured body when the accepted
 * generation or one of its snapshots is not there yet. That is a normal state
 * of the planning truth (fail closed), so it is shown as a warning with the
 * backend's own reason instead of a red transport error. */
export function TruthUnavailableNotice({ reason }: Props) {
  return (
    <div className="warningLine" role="status">
      <strong>Правда недоступна:</strong> {reason}
    </div>
  )
}
