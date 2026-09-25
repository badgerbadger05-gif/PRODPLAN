from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.purchase_control_materialization import (
    PurchaseControlMaterializationError,
    PurchaseControlMaterializerNotConfigured,
    PurchaseControlSnapshotUnavailable,
    materialize_rows,
)
from ..services.purchase_control_journal import (
    get_order_card,
    get_selection_summary,
    _selection_summary_from_rows,
    apply_materialization_action,
    list_filters,
    list_journal,
    purchase_journal_meta,
    purchase_journal_summary,
)
from ..services.purchase_control_projection import PurchaseJournalUnavailable
from ..services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    require_current_execution_scope,
)

router = APIRouter(prefix="/v1/purchase-control", tags=["purchase-control"])


class PurchaseControlMaterializeRequest(BaseModel):
    current_scope_id: Optional[int] = Field(default=None, ge=1)
    row_keys: list[str] = Field(default_factory=list)
    dry_run: bool = True
    current_identity: Optional[str] = None
    current_identities: list[str] = Field(default_factory=list, max_length=500)
    expected_source_revision: Optional[str] = None


class PurchaseControlSelectionSummaryRequest(BaseModel):
    current_scope_id: Optional[int] = Field(default=None, ge=1)
    row_keys: list[str] = Field(default_factory=list, max_length=500)
    horizon_period_to: Optional[date] = None
    current_identity: Optional[str] = None
    current_identities: list[str] = Field(default_factory=list, max_length=500)
    expected_source_revision: Optional[str] = None


class PurchaseControlSelectionSummaryResponse(BaseModel):
    current_scope_id: int
    selected_rows: int
    priced_rows: int
    unpriced_rows: int
    known_amount: float
    total_amount: Optional[float] = None
    amount_status: Literal["complete", "partial", "unavailable"]
    current_identity: Optional[str] = None
    current_identities: list[str] = Field(default_factory=list)
    source_revision: Optional[str] = None


def _resolve_current_purchase_selection(
    db: Session,
    *,
    current_scope_id: int | None,
    row_keys: list[str],
    current_identity: str | None,
    current_identities: list[str],
    expected_source_revision: str | None,
) -> tuple[object, list[dict], list[str]]:
    manifest = require_current_execution_scope(
        db,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )
    if current_scope_id is not None and int(current_scope_id) != int(manifest.id):
        raise ValueError("Текущий manifest изменился; обновите страницу и повторите выбор")
    if expected_source_revision is not None and str(expected_source_revision) != str(manifest.source_revision):
        raise ValueError("Текущая ревизия закупок устарела; обновите страницу и повторите выбор")
    if not expected_source_revision:
        raise ValueError("expected_source_revision обязателен для current selection")
    current = load_current_execution_rows(
        db,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )
    by_identity = {str(row.business_identity): row for row in current}
    selected = list(dict.fromkeys(str(key or "").strip() for key in row_keys if str(key or "").strip()))
    if current_identity:
        selected.append(str(current_identity))
    selected.extend(str(identity or "").strip() for identity in current_identities if str(identity or "").strip())
    selected = list(dict.fromkeys(selected))
    if not selected:
        raise ValueError("Не выбраны строки журнала закупок")
    rows: list[dict] = []
    resolved_keys: list[str] = []
    for key in selected:
        row = by_identity.get(key)
        if row is None:
            row = next(
                (
                    candidate for candidate in current
                    if str((candidate.payload or {}).get("row_key") or "") == key
                ),
                None,
            )
        if row is None:
            raise ValueError("Выбранная строка отсутствует в current purchase journal")
        payload = dict(row.payload or {})
        payload["row_key"] = str(row.business_identity)
        rows.append(payload)
        resolved_keys.append(str(row.business_identity))
    return manifest, rows, resolved_keys


def _canonical_purchase_sort(rows: list[dict], *, field: str, descending: bool) -> None:
    """Sort purchase rows with typed ties and NULLS LAST."""

    def _typed(value: object, key: str) -> tuple[int, object]:
        if value in (None, ""):
            return (9, "")
        if key in {"delivery_date", "order_date"}:
            # ISO dates sort chronologically and preserve date/datetime inputs.
            return (0, value.isoformat() if hasattr(value, "isoformat") else str(value))
        if key in {"remaining_qty", "to_order_qty", "quantity", "amount"}:
            try:
                return (0, Decimal(str(value)))
            except (InvalidOperation, TypeError, ValueError):
                return (1, str(value))
        if key == "order_number":
            try:
                return (0, int(str(value)))
            except (TypeError, ValueError):
                return (1, str(value))
        return (0, str(value))

    def _tie(row: dict) -> tuple[int, object, str, str]:
        raw_order = row.get("order_number")
        try:
            order_key: object = int(raw_order)
            numeric = 0
        except (TypeError, ValueError):
            order_key = str(raw_order or "")
            numeric = 1
        return (
            numeric,
            order_key,
            str(row.get("line_number") or ""),
            str(row.get("row_key") or ""),
        )

    rows.sort(key=_tie)
    present = [row for row in rows if row.get(field) not in (None, "")]
    missing = [row for row in rows if row.get(field) in (None, "")]
    present.sort(key=lambda row: _typed(row.get(field), field), reverse=descending)
    rows[:] = present + missing


@router.get("/orders", response_model=dict)
def get_orders(
    order_id: Optional[int] = None,
    supplier_id: Optional[int] = None,
    state: Optional[str] = None,
    phase: Optional[str] = None,
    line_status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    active_only: bool = True,
    include_to_order: bool = True,
    horizon_period_to: Optional[date] = Query(
        None,
        description=(
            "Горизонт формирования заказов: показывать 'to_order'-строки только "
            "по активным прогонам, чей план заканчивается не позже этой даты "
            "(ISO). None = весь горизонт (все активные прогоны)."
        ),
    ),
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_identity: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Журнал закупок: строки заказов поставщику (1С) + незаказанные MRP-потребности
    (`line_status = to_order`) последнего FIXED_SNAPSHOT-прогона.
    """
    try:
        from ..services.item_ledger.current_execution import (
            CurrentExecutionUnavailable,
            load_current_execution_rows,
            require_current_execution_scope,
        )
        current_manifest = require_current_execution_scope(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        current_rows = load_current_execution_rows(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        if current_identity is not None:
            target_identity = str(current_identity).strip()
            current_rows = [
                current_row for current_row in current_rows
                if str(current_row.business_identity) == target_identity
            ]
        rows = []
        for current_row in current_rows:
            payload = dict(current_row.payload or {})
            payload["current_identity"] = str(current_row.business_identity)
            # The manifest revision is the CAS token.  Current row provenance
            # may intentionally remain unchanged across a technical no-op
            # publication, so never expose it as the action revision.
            payload["source_revision"] = str(current_manifest.source_revision)
            rows.append(payload)
        if horizon_period_to is not None:
            from ..services.purchase_control_journal import _reconcile_buy_row_for_horizon
            rows = [
                projected
                for row in rows
                for projected in [_reconcile_buy_row_for_horizon(row, horizon_period_to.isoformat())]
                if projected is not None
            ]
        # One canonical materialization rule, stamped on the projected row and
        # before any row is filtered out: the export endpoint re-checks the very
        # same rule, so the journal must not publish a second answer.
        apply_materialization_action(rows)
        if order_id is not None:
            rows = [row for row in rows if row.get("order_id") == int(order_id)]
        if supplier_id is not None:
            rows = [row for row in rows if row.get("supplier_id") == int(supplier_id)]
        if state:
            rows = [row for row in rows if str(row.get("order_state_name") or "") == str(state)]
        if phase:
            rows = [row for row in rows if str(row.get("supply_phase") or "") == str(phase)]
        if line_status:
            rows = [row for row in rows if str(row.get("line_status") or "") == str(line_status)]
        if not include_to_order:
            rows = [row for row in rows if row.get("line_status") != "to_order"]
        if active_only:
            rows = [row for row in rows if float(row.get("remaining_qty") or 0) > 0]
        if search:
            needle = str(search).casefold()
            rows = [row for row in rows if needle in " ".join(
                str(row.get(key) or "") for key in ("order_number", "item_name", "item_code", "item_article", "supplier_name")
            ).casefold()]
        if date_from:
            rows = [row for row in rows if row.get("delivery_date") is not None and str(row["delivery_date"]) >= str(date_from)]
        if date_to:
            rows = [row for row in rows if row.get("delivery_date") is not None and str(row["delivery_date"]) <= str(date_to)]
        sort_key = sort_by if sort_by in {"delivery_date", "order_date", "order_number", "item_code", "remaining_qty"} else "delivery_date"
        _canonical_purchase_sort(
            rows,
            field=sort_key,
            descending=str(sort_dir or "asc").casefold() == "desc",
        )
        effective_limit = max(1, min(int(limit or 100), 500))
        effective_offset = max(0, int(offset or 0))
        # Read-time metadata of the served scope.  The stored envelope keeps the
        # status captured while the candidate was still BUILDING; the reader
        # reports the readiness of the accepted pointer it was served from.
        saved = purchase_journal_meta(current_manifest)
        saved.pop("snapshot_id", None)
        saved.pop("summary", None)
        saved.pop("cards", None)
        saved["current_execution_scope_id"] = int(current_manifest.id)
        # The page totals belong to the rows this answer serves, not to the
        # unfiltered build-time cardinality of the publication envelope.
        page_summary = purchase_journal_summary(rows)
        saved_buckets = [
            dict(bucket)
            for bucket in list(saved.get("to_order_by_period") or [])
            if isinstance(bucket, dict)
        ]
        if horizon_period_to is not None:
            horizon_iso = horizon_period_to.isoformat()
            saved_buckets = [
                bucket for bucket in saved_buckets
                if str(bucket.get("plan_period_to") or "") <= horizon_iso
            ]
        for bucket in saved_buckets:
            bucket.setdefault("period_to", bucket.get("plan_period_to"))
        return {
                "rows": rows[effective_offset:effective_offset + effective_limit],
                "total": len(rows),
                "limit": effective_limit,
                "offset": effective_offset,
                "run_id": saved.get("run_id"),
                "run_ids": list(saved.get("run_ids") or []),
                "truth_status": saved["truth_status"],
                "ledger_generation_id": current_manifest.source_generation_id,
                "source_revision": str(current_manifest.source_revision),
                "current_identity": None,
                "to_order_by_period": saved_buckets,
                "summary": page_summary,
                "meta": saved,
        }
    except PurchaseJournalUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except CurrentExecutionUnavailable as e:
        raise HTTPException(status_code=503, detail={"code": "purchase_control_current_unavailable", "reason": str(e)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}", response_model=dict)
def get_order(order_id: int, db: Session = Depends(get_db)):
    """Карточка заказа поставщику со всеми строками (для detail pane)."""
    try:
        from ..services.item_ledger.current_execution import (
            CurrentExecutionUnavailable,
            load_current_execution_rows,
            require_current_execution_scope,
        )
        manifest = require_current_execution_scope(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        cards = dict(manifest.summary or {}).get("cards")
        if isinstance(cards, dict) and str(int(order_id)) in cards:
            # Same read-time truth rule as the journal: a card served from the
            # accepted pointer is accepted, never the stored build-time status.
            meta = purchase_journal_meta(manifest)
            meta.pop("summary", None)
            meta.pop("cards", None)
            meta["current_execution_scope_id"] = int(manifest.id)
            return {**dict(cards[str(int(order_id))]), "meta": meta}
        raise ValueError(f"Supplier order {order_id} card is not published in current purchase journal")
    except PurchaseJournalUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except CurrentExecutionUnavailable as e:
        raise HTTPException(status_code=503, detail={"code": "purchase_control_current_unavailable", "reason": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/filters", response_model=dict)
def get_filters(db: Session = Depends(get_db)):
    """Справочники для фильтров журнала: поставщики и состояния заказов 1С."""
    try:
        from ..services.item_ledger.current_execution import (
            CurrentExecutionUnavailable,
            load_current_execution_rows,
            require_current_execution_scope,
        )
        manifest = require_current_execution_scope(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        rows = [dict(row.payload or {}) for row in load_current_execution_rows(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )]
        saved = dict(manifest.summary or {})
        card_rows = [
            line
            for card in (saved.get("cards") or {}).values()
            if isinstance(card, dict)
            for line in (card.get("lines") or [])
            if isinstance(line, dict)
        ]
        all_rows = [*rows, *card_rows]
        suppliers = sorted({
            (int(row["supplier_id"]), str(row.get("supplier_name") or ""))
            for row in all_rows if row.get("supplier_id") is not None
        }, key=lambda value: (value[1].casefold(), value[0]))
        states = sorted({
            str(row["order_state_name"])
            for row in all_rows if row.get("order_state_name")
        })
        return {
            "suppliers": [{"supplier_id": value, "supplier_name": name} for value, name in suppliers],
            "states": states,
        }
    except PurchaseJournalUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except CurrentExecutionUnavailable as e:
        raise HTTPException(status_code=503, detail={"code": "purchase_control_current_unavailable", "reason": str(e)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/selection-summary",
    response_model=PurchaseControlSelectionSummaryResponse,
)
def summarize_purchase_control_selection(
    payload: PurchaseControlSelectionSummaryRequest,
    db: Session = Depends(get_db),
):
    """Backend-owned totals for the selected immutable purchase rows."""
    try:
        manifest, rows, identities = _resolve_current_purchase_selection(
            db,
            current_scope_id=payload.current_scope_id,
            row_keys=payload.row_keys,
            current_identity=payload.current_identity,
            current_identities=payload.current_identities,
            expected_source_revision=payload.expected_source_revision,
        )
        result = _selection_summary_from_rows(
            rows=rows,
            current_scope_id=int(manifest.id),
            row_keys=[str(row.get("row_key")) for row in rows],
            horizon_period_to=payload.horizon_period_to,
        )
        result["current_identity"] = payload.current_identity
        result["current_identities"] = identities
        result["source_revision"] = str(manifest.source_revision)
        return result
    except PurchaseJournalUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/materialize", response_model=dict)
def materialize_purchase_control_rows(
    payload: PurchaseControlMaterializeRequest,
    db: Session = Depends(get_db),
):
    """Materialize selected neutral MRP purchase rows from the accepted snapshot."""
    try:
        manifest, rows, identities = _resolve_current_purchase_selection(
            db,
            current_scope_id=payload.current_scope_id,
            row_keys=payload.row_keys,
            current_identity=payload.current_identity,
            current_identities=payload.current_identities,
            expected_source_revision=payload.expected_source_revision,
        )
        return materialize_rows(
            db,
            current_scope_id=int(manifest.id),
            row_keys=identities,
            dry_run=payload.dry_run,
            current_manifest=manifest,
            current_rows=rows,
        )
    except PurchaseControlSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail)
    except PurchaseJournalUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except PurchaseControlMaterializerNotConfigured as e:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "purchase_control_materializer_not_configured",
                "consumer": "purchase_control",
                "reason": str(e),
            },
        )
    except PurchaseControlMaterializationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
