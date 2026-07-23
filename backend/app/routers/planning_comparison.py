from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import planning_comparison


router = APIRouter(prefix="/v1/planning-comparison", tags=["planning-comparison"])


class CaptureRequest(BaseModel):
    capture_key: Optional[str] = Field(default=None, min_length=1, max_length=128)
    max_skew_seconds: int = Field(default=300, ge=0, le=86400)


@router.get("/input-fingerprint", response_model=dict)
def get_input_fingerprint(
    include_results: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Read-only comparable input watermarks and optional canonical latest results."""
    try:
        return planning_comparison.input_fingerprint(db, include_results=include_results)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/captures", response_model=dict)
def create_capture(payload: CaptureRequest, db: Session = Depends(get_db)):
    """Capture stable through HTTP and shadow locally; never starts/materializes planning."""
    try:
        return planning_comparison.capture(
            db, capture_key=payload.capture_key, max_skew_seconds=payload.max_skew_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/batches", response_model=dict)
def get_batches(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    return planning_comparison.list_batches(db, limit=limit, offset=offset)


@router.get("/batches/{batch_id}", response_model=dict)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    try:
        return planning_comparison.batch_detail(db, batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
