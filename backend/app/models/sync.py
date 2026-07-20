from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from ..database import Base
from .types import CrossPlatformJSON


class SyncLink(Base):
    """
    Idempotency table for PRODPLAN <-> 1C document exchange. See
    .docs/one_c_export_from_prodplan.md ("Идемпотентность"). Each export
    service upserts one row per (source_doctype, source_id, target_entity)
    and stores the 1C Ref_Key on success so a re-run is a no-op.
    """
    __tablename__ = "sync_link"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_doctype",
            "source_id",
            "target_entity",
            name="ux_sync_link_source_target",
        ),
    )

    link_id = Column(Integer, primary_key=True, index=True)
    source_system = Column(String(50), nullable=False, default="PRODPLAN", server_default="PRODPLAN")
    source_doctype = Column(String(50), nullable=False)
    source_id = Column(Integer, nullable=False)
    target_system = Column(String(50), nullable=False, default="1C", server_default="1C")
    target_entity = Column(String(100), nullable=False, index=True)
    target_ref_key = Column(String(36), nullable=True, index=True)
    target_number = Column(String(50), nullable=True)
    payload_hash = Column(String(64), nullable=True)
    # planned | success | error | cancelled
    status = Column(String(20), nullable=False, default="planned", server_default="planned", index=True)
    last_error = Column(TEXT, nullable=True)
    last_synced_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False)
