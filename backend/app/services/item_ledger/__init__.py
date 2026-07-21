"""Item-ledger subsystem (design §2–§6) — Increment 1.

Pure/standalone fold and distribution math for the two-ledger stock +
reservation model. NOTHING here is wired into freeze / cycle / reconcile in
Inc1: no reader consults these functions and no writer calls them from the
planning pipeline — the module is additive and side-effect-free except where a
Session is explicitly passed (rebuild_running_balance, seed_from_balance,
fold_reservation_entry), which only touch the new ledger tables.

The distribution core (`redistribute`) is a PURE function of its inputs: same
Pool in → byte-identical coverage out (INV-idem-dist, design §9).
"""

from .physical import (  # noqa: F401
    EPS,
    LedgerKey,
    fold_running_balance,
    rebuild_running_balance,
    seed_from_balance,
)
from .ingest import (  # noqa: F401
    INGEST_SOURCE,
    REGISTER_ENTITY,
    PullResult,
    enqueue_recorder_pull,
    process_pending_pulls,
    pull_recorder_movements,
)
from .reservation import (  # noqa: F401
    Coverage,
    IncomingLine,
    Pin,
    Pool,
    RedistributeResult,
    Reserve,
    ReservationFold,
    available,
    coverage_state_for,
    fold_reservation_entry,
    fold_reservation_events,
    incoming,
    make_materialization_gap,
    make_uncovered,
    projected,
    redistribute,
    reserved_soft,
    uncovered_pool,
)
