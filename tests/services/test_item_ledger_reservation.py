"""Ledger-2 golden + invariant tests (design §5, §7 examples 1–5, §9).

Every intermediate number of the §7 worked-example tables is asserted:
available / projected / reserved_soft / uncovered(P) and the per-reserve
coverage (covered_on_hand / covered_incoming_* / uncovered / coverage_state).
Plus the §9 invariants (INV-RES-fold, INV-RES-cons, INV-RES-noverbook,
INV-RES-uncov, INV-RES-make-zero, INV-RES-onemode, INV-idem-dist) and a
cumulative-no-overbook property test.
"""

import random
from datetime import date
from decimal import Decimal

import pytest

from app.services.item_ledger import (
    IncomingLine,
    Pin,
    Pool,
    Reserve,
    available,
    coverage_state_for,
    fold_reservation_events,
    incoming,
    make_materialization_gap,
    make_uncovered,
    projected,
    redistribute,
    reserved_soft,
    uncovered_pool,
)

D = Decimal

# K(r) = (priority_period_from, priority_period_to, run_id, requirement_id) — §3.
K_R1 = (date(2026, 7, 1), date(2026, 7, 15), 21, 501)
K_R2 = (date(2026, 7, 16), date(2026, 7, 31), 22, 502)
K_R3 = (date(2026, 8, 1), date(2026, 8, 15), 23, 503)
K_R5 = (date(2026, 7, 1), date(2026, 7, 15), 24, 505)


def _reserve(key, reserved, realized=0, mode="consume", pins=None, **kw):
    return Reserve(
        key=key,
        reserved_qty=D(str(reserved)),
        realized_qty=D(str(realized)),
        realization_mode=mode,
        pins=list(pins or []),
        **kw,
    )


def _by_key(result, key):
    return next(r for r in result.reserves if r.key == key)


def _f(x):
    return float(x)


# ---------------------------------------------------------------------------
# INV-RES-fold (§2.3 / §9)
# ---------------------------------------------------------------------------


def test_inv_res_fold_pure():
    # open +6, amend -2 (reserved=4), realize +4 (realized=4) → outstanding 0.
    fold = fold_reservation_events([(6, 0), (-2, 0), (0, 4)])
    assert _f(fold.reserved_qty) == 4
    assert _f(fold.realized_qty) == 4
    assert _f(fold.outstanding) == 0


def test_inv_res_fold_outstanding_clamped_nonnegative():
    fold = fold_reservation_events([(5, 0), (0, 8)])  # over-realized
    assert _f(fold.reserved_qty) == 5
    assert _f(fold.realized_qty) == 8
    assert _f(fold.outstanding) == 0  # max(reserved-realized, 0)


# ---------------------------------------------------------------------------
# Example 1 — end-to-end (§7)
# ---------------------------------------------------------------------------


def test_example1_step0_initial():
    pool = Pool(on_hand=10, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 5), _reserve(K_R3, 4)])
    assert _f(reserved_soft(pool)) == 15
    assert _f(available(pool)) == -5
    assert _f(projected(pool)) == -5
    assert _f(incoming(pool)) == 0
    assert _f(uncovered_pool(pool)) == 5

    res = redistribute(pool)
    r1, r2, r3 = _by_key(res, K_R1), _by_key(res, K_R2), _by_key(res, K_R3)
    assert (_f(r1.covered_on_hand), _f(r1.uncovered), r1.coverage_state) == (6, 0, "covered")
    assert (_f(r2.covered_on_hand), _f(r2.uncovered), r2.coverage_state) == (4, 1, "partial")
    assert (_f(r3.covered_on_hand), _f(r3.uncovered), r3.coverage_state) == (0, 4, "uncovered")


def test_example1_step1_supplier_order_no_pin():
    line = IncomingLine("po8", "supplier_order", 8, due_date=date(2026, 7, 20), order_ref="PO8")
    pool = Pool(on_hand=10, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 5), _reserve(K_R3, 4)], lines=[line])
    assert _f(reserved_soft(pool)) == 15
    assert _f(available(pool)) == -5
    assert _f(projected(pool)) == 3
    assert _f(incoming(pool)) == 8
    assert _f(uncovered_pool(pool)) == 0

    res = redistribute(pool)
    r1, r2, r3 = _by_key(res, K_R1), _by_key(res, K_R2), _by_key(res, K_R3)
    # B: 6/4/0 (on_hand); C: R2 +1, R3 +4 (free line); free_line = 3.
    assert (_f(r1.covered_on_hand), _f(r1.covered_incoming_supplier)) == (6, 0)
    assert (_f(r2.covered_on_hand), _f(r2.covered_incoming_supplier)) == (4, 1)
    assert (_f(r3.covered_on_hand), _f(r3.covered_incoming_supplier)) == (0, 4)
    assert all(r.coverage_state == "covered" for r in res.reserves)
    assert _f(line.remaining) == 8  # redistribute must NOT mutate the input line


def test_example1_step2_physical_receipt():
    pool = Pool(on_hand=18, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 5), _reserve(K_R3, 4)])
    assert _f(reserved_soft(pool)) == 15
    assert _f(available(pool)) == 3
    assert _f(projected(pool)) == 3
    assert _f(uncovered_pool(pool)) == 0

    res = redistribute(pool)
    r1, r2, r3 = _by_key(res, K_R1), _by_key(res, K_R2), _by_key(res, K_R3)
    assert (_f(r1.covered_on_hand), _f(r2.covered_on_hand), _f(r3.covered_on_hand)) == (6, 5, 4)
    assert all(r.coverage_state == "covered" for r in res.reserves)


def test_example1_step3_assembly_out_pegged_r1():
    # realize(R1,+5): outstanding(R1)=1; on_hand 13.
    pool = Pool(
        on_hand=13,
        reserves=[_reserve(K_R1, 6, realized=5), _reserve(K_R2, 5), _reserve(K_R3, 4)],
    )
    assert _f(reserved_soft(pool)) == 10  # 1 + 5 + 4
    assert _f(available(pool)) == 3
    assert _f(projected(pool)) == 3
    assert _f(uncovered_pool(pool)) == 0

    res = redistribute(pool)
    r1, r2, r3 = _by_key(res, K_R1), _by_key(res, K_R2), _by_key(res, K_R3)
    assert (_f(r1.outstanding), _f(r1.covered_on_hand), r1.coverage_state) == (1, 1, "covered")
    assert (_f(r2.covered_on_hand), _f(r3.covered_on_hand)) == (5, 4)


# ---------------------------------------------------------------------------
# Example 2 — over-reservation (§7)
# ---------------------------------------------------------------------------


def test_example2_over_reservation():
    pool = Pool(on_hand=4, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 5)])
    assert _f(reserved_soft(pool)) == 11
    assert _f(available(pool)) == -7  # surfaced, never clamped
    assert _f(uncovered_pool(pool)) == 7

    res = redistribute(pool)
    r1, r2 = _by_key(res, K_R1), _by_key(res, K_R2)
    assert (_f(r1.covered_on_hand), _f(r1.uncovered), r1.coverage_state) == (4, 2, "partial")
    assert (_f(r2.covered_on_hand), _f(r2.uncovered), r2.coverage_state) == (0, 5, "uncovered")
    # after export: incoming_wip=7 → uncovered 0, projected 0.
    covered = Pool(
        on_hand=4,
        reserves=[_reserve(K_R1, 6), _reserve(K_R2, 5)],
        lines=[IncomingLine("wip7", "wip_order", 7, due_date=date(2026, 7, 10))],
    )
    assert _f(projected(covered)) == 0
    res2 = redistribute(covered)
    assert all(r.coverage_state == "covered" for r in res2.reserves)
    assert _f(available(covered)) == -7  # available stays negative until physical


# ---------------------------------------------------------------------------
# Example 3 — unplanned consumption (§7)
# ---------------------------------------------------------------------------


def test_example3_unplanned_consumption():
    before = Pool(on_hand=10, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 4)])
    assert _f(available(before)) == 0
    res0 = redistribute(before)
    assert all(r.coverage_state == "covered" for r in res0.reserves)

    # adjustment SLE -3 (not matched) → on_hand 7.
    after = Pool(on_hand=7, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 4)])
    assert _f(available(after)) == -3
    assert _f(projected(after)) == -3
    assert _f(uncovered_pool(after)) == 3

    res = redistribute(after)
    r1, r2 = _by_key(res, K_R1), _by_key(res, K_R2)
    assert (_f(r1.covered_on_hand), r1.coverage_state) == (6, "covered")
    assert (_f(r2.covered_on_hand), _f(r2.uncovered), r2.coverage_state) == (1, 3, "partial")


# ---------------------------------------------------------------------------
# Example 4 — pin evaporation (§7)
# ---------------------------------------------------------------------------


def test_example4_pin_evaporation():
    # pinned: supplier order line 4 pins R3 → covered.
    pinned = Pool(
        on_hand=0,
        reserves=[_reserve(K_R3, 4, pins=[Pin("po4", "supplier_order", 4)])],
        lines=[IncomingLine("po4", "supplier_order", 4, due_date=date(2026, 8, 1))],
    )
    res = redistribute(pinned)
    r3 = _by_key(res, K_R3)
    assert (_f(r3.covered_incoming_supplier), _f(r3.uncovered), r3.coverage_state) == (4, 0, "covered")
    assert _f(projected(pinned)) == 0  # 0 + 4 - 4

    # order cancelled: pin evaporated (pin_live 0), open line gone.
    evaporated = Pool(
        on_hand=0,
        reserves=[_reserve(K_R3, 4, pins=[Pin("po4", "supplier_order", 4, evaporated_qty=4)])],
        lines=[],
    )
    res2 = redistribute(evaporated)
    r3b = _by_key(res2, K_R3)
    assert (_f(r3b.covered_incoming_supplier), _f(r3b.uncovered), r3b.coverage_state) == (0, 4, "uncovered")
    assert _f(projected(evaporated)) == -4  # incoming −4 ⇒ projected −4


# ---------------------------------------------------------------------------
# Example 5 — finished goods (make-reserve, §7 / §3.1)
# ---------------------------------------------------------------------------


def test_example5_make_reserve_finished_goods():
    # step 0: freeze, reserved=5, produced=0.
    r5 = _reserve(K_R5, 5, mode="make")
    pool = Pool(on_hand=0, reserves=[r5])
    assert _f(reserved_soft(pool)) == 0  # INV-RES-make-zero
    assert _f(available(pool)) == 0
    assert _f(make_uncovered(r5)) == 5  # → proposal

    # make reserves never participate in distribution.
    res = redistribute(pool)
    assert res.reserves == []
    assert _f(r5.covered_on_hand) == 0
    assert _f(reserved_soft(pool)) == 0 and _f(available(pool)) == 0

    # step 1: production order Z on 5 (pegged) → own_open_supply = 5.
    r5.own_open_supply_qty = D("5")
    assert _f(make_uncovered(r5)) == 5  # supplier pins only; wip is own-open
    assert _f(make_materialization_gap(r5)) == 0  # G2 — no re-proposal
    assert _f(available(pool)) == 0

    # step 2: release +5 to finished-goods warehouse (pegged → R5).
    r5.produced_qty = D("5")
    assert _f(make_uncovered(r5)) == 0
    assert r5.produced_qty >= r5.reserved_qty  # closure: produced ≥ reserved → closed
    assert _f(available(pool)) == 0  # pool untouched on every step


# ---------------------------------------------------------------------------
# §9 invariants
# ---------------------------------------------------------------------------


def test_inv_res_onemode_and_make_zero():
    # A produced intermediate node: consume + make are DISTINCT records; only
    # consume contributes to reserved_soft (§2.2, INV-RES-onemode).
    consume = _reserve(K_R1, 6, mode="consume")
    make = _reserve(K_R1, 6, mode="make")
    pool = Pool(on_hand=10, reserves=[consume, make])
    assert _f(reserved_soft(pool)) == 6  # make adds exactly 0


def test_inv_idem_dist_double_run_identical():
    def _mk():
        return Pool(
            on_hand=10,
            reserves=[_reserve(K_R1, 6), _reserve(K_R2, 5), _reserve(K_R3, 4)],
            lines=[IncomingLine("po8", "supplier_order", 8, due_date=date(2026, 7, 20))],
        )

    a = _mk()
    r1 = redistribute(a)
    r2 = redistribute(a)  # re-run on the SAME (unmutated) pool
    snap1 = [(r.key, _f(r.covered_on_hand), _f(r.covered_incoming_supplier), _f(r.uncovered)) for r in r1.reserves]
    snap2 = [(r.key, _f(r.covered_on_hand), _f(r.covered_incoming_supplier), _f(r.uncovered)) for r in r2.reserves]
    assert snap1 == snap2


def test_inv_res_uncov_identity_holds_on_examples():
    pools = [
        Pool(on_hand=10, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 5), _reserve(K_R3, 4)]),
        Pool(on_hand=4, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 5)]),
        Pool(on_hand=7, reserves=[_reserve(K_R1, 6), _reserve(K_R2, 4)]),
    ]
    for pool in pools:
        res = redistribute(pool)  # raises internally if INV-RES-uncov breaks
        total = sum((r.uncovered for r in res.reserves), D("0"))
        assert abs(total - uncovered_pool(pool)) <= D("1e-9")


def test_coverage_state_helper():
    assert coverage_state_for(0, 0) == "covered"
    assert coverage_state_for(5, 5) == "covered"
    assert coverage_state_for(5, 2) == "partial"
    assert coverage_state_for(5, 0) == "uncovered"


# ---------------------------------------------------------------------------
# Property test — cumulative no-overbook (§9): each unit covered ≤ 1×
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", list(range(40)))
def test_property_cumulative_no_overbook(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 6)
    reserves = []
    for i in range(n):
        key = (date(2026, 7, 1), date(2026, 7, 15), 20 + i, 500 + i)
        reserved = rng.randint(0, 12)
        realized = rng.randint(0, reserved) if reserved else 0
        reserves.append(_reserve(key, reserved, realized=realized))

    lines = []
    for j in range(rng.randint(0, 4)):
        kind = rng.choice(["supplier_order", "wip_order"])
        lines.append(
            IncomingLine(f"L{j}", kind, rng.randint(0, 10), due_date=date(2026, 7, rng.randint(1, 28)))
        )
    pool = Pool(on_hand=rng.randint(-5, 20), reserves=reserves, lines=lines)

    res = redistribute(pool)  # internal asserts enforce per-line + on_hand caps

    oh_pos = pool.on_hand if pool.on_hand > 0 else D("0")
    assert sum((r.covered_on_hand for r in res.reserves), D("0")) <= oh_pos + D("1e-9")
    assert (
        sum((r.covered_incoming_supplier + r.covered_incoming_wip for r in res.reserves), D("0"))
        <= incoming(pool) + D("1e-9")
    )
    for r in res.reserves:
        assert r.covered <= r.outstanding + D("1e-9")

    total_uncovered = sum((r.uncovered for r in res.reserves), D("0"))
    assert abs(total_uncovered - uncovered_pool(pool)) <= D("1e-9")

    # idempotency on the same pool.
    res2 = redistribute(pool)
    assert [_f(r.covered) for r in res.reserves] == [_f(r.covered) for r in res2.reserves]
