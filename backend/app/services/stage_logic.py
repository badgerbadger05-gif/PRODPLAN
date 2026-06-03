from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple
from collections import defaultdict


def determine_parent_stage_and_norm(
    default_spec_map: Dict[int, int],
    get_components_for_spec: Callable[[int], List],
    get_operations_for_spec: Callable[[int], List],
    item_id: int,
) -> Tuple[Optional[int], Optional[str], float]:
    """
    Determine stage_id for a PARENT item based on its own specification children,
    aligned with Resources 'Распределение этапов' logic, with fallback by operations.

    Strategy (aligned with previous MRP logic and resource calculator):
      - If item has no default spec: stage=None, reason='NO_DEFAULT_SPEC'
      - Read components of the parent's spec (only this level):
         * If exactly one unique child.stage_id exists -> parent_stage = that id
         * If none -> reason='NO_CHILD_STAGE'
         * If mixed -> reason='MIXED_CHILD_STAGES'
      - Fallback by operations: if stage_id is None (NO_CHILD_STAGE or MIXED_CHILD_STAGES),
        then compute a majority by SpecOperation.stage_id. If a unique majority exists,
        use it and set reason='FROM_OPERATIONS'.
      - Norm-hours per unit = sum(time_norm) for all operations of the parent spec
      - Leaves (no components): reason='NO_CHILDREN', stage=None, norm = sum(ops)

    Returns: (stage_id or None, reason or None, norm_hours_single: float)
    """
    spec_id = default_spec_map.get(int(item_id))
    if not spec_id:
        return None, "NO_DEFAULT_SPEC", 0.0

    comps = get_components_for_spec(int(spec_id)) or []

    # Sum operation time for this parent spec
    ops = get_operations_for_spec(int(spec_id)) or []
    try:
        norm_hours_single = float(sum(float(getattr(o, "time_norm", 0.0) or 0.0) for o in ops))
    except Exception:
        norm_hours_single = 0.0

    if not comps:
        # Leaf: parent has no children – try to infer stage from operations majority
        op_stage_counts: Dict[int, int] = {}
        try:
            from collections import defaultdict as _dd
            cnt = _dd(int)
            for op in ops:
                sid = getattr(op, "stage_id", None)
                if sid is None:
                    continue
                try:
                    cnt[int(sid)] += 1
                except Exception:
                    continue
            op_stage_counts = dict(cnt)
        except Exception:
            op_stage_counts = {}

        if op_stage_counts:
            max_cnt = max(op_stage_counts.values())
            top = [sid for sid, c in op_stage_counts.items() if c == max_cnt]
            if len(top) == 1:
                # Unique majority stage by operations for leaf
                return int(top[0]), "FROM_OPERATIONS_LEAF", norm_hours_single

        # Fallback: keep original behavior
        return None, "NO_CHILDREN", norm_hours_single

    # Collect child stage_ids (this level only)
    child_stage_ids: Set[int] = set()
    for c in comps:
        sid = getattr(c, "stage_id", None)
        if sid is None:
            continue
        try:
            child_stage_ids.add(int(sid))
        except Exception:
            continue

    stage_id: Optional[int] = None
    reason: Optional[str] = None
    if len(child_stage_ids) == 1:
        stage_id = next(iter(child_stage_ids))
    elif len(child_stage_ids) == 0:
        reason = "NO_CHILD_STAGE"
    else:
        reason = "MIXED_CHILD_STAGES"

    # Fallback by operations majority when child stages are absent or mixed
    if stage_id is None and ops:
        counts: Dict[int, int] = defaultdict(int)
        for op in ops:
            sid = getattr(op, "stage_id", None)
            if sid is None:
                continue
            try:
                counts[int(sid)] += 1
            except Exception:
                continue
        if counts:
            max_cnt = max(counts.values())
            top = [sid for sid, cnt in counts.items() if cnt == max_cnt]
            if len(top) == 1:
                stage_id = int(top[0])
                reason = "FROM_OPERATIONS"

    return stage_id, reason, norm_hours_single
