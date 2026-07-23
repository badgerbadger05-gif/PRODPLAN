import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DbrBoard, DbrBoardSlot } from '../../../domain/dbr'
import { dbrSnapshotUnavailableMessage, getDbrBoard } from '../../../services/dbr'
import { groupDrumSlotsByCell, indexDrumSlotsById } from './model'

export function useDbrDrumBoardController() {
  const requestRef = useRef(0)
  const [board, setBoard] = useState<DbrBoard | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selectedSlot, setSelectedSlot] = useState<DbrBoardSlot | null>(null)

  // The board is one immutable accepted Ledger snapshot.  Its mount performs
  // exactly one GET; local slot inspection never invokes calculation.
  const load = useCallback(async () => {
    const request = ++requestRef.current
    setLoading(true); setError('')
    try {
      const next = await getDbrBoard()
      if (request === requestRef.current) setBoard(next)
    } catch (cause) {
      if (request === requestRef.current) {
        setBoard(null)
        setError(dbrSnapshotUnavailableMessage(cause) ?? (cause instanceof Error ? cause.message : String(cause)))
      }
    } finally {
      if (request === requestRef.current) setLoading(false)
    }
  }, [])
  useEffect(() => { void load() }, [load])

  return {
    board, loading, error, selectedSlot, setSelectedSlot, load,
    schedule: board?.schedule ?? null,
    slotsByCell: useMemo(() => groupDrumSlotsByCell(board?.slots ?? []), [board]),
    slotById: useMemo(() => indexDrumSlotsById(board?.slots ?? []), [board]),
  }
}
