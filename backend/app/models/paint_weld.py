from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT, Boolean, DateTime, Date, CheckConstraint, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from ..database import Base
from .types import CrossPlatformJSON


class PaintWeldPair(Base):
    """
    Связка «окрашенная ↔ сварная (неокрашенная)» деталь для семейства
    «… после покраски». Строится автоматически из default-спек (source='auto'),
    ручные правки допустимы (source='manual'). См. .docs/paint_weld_chain_logic.md.

    Одна окрашенная деталь = одна сварная (painted_item_id UNIQUE). Сварная может
    участвовать в нескольких парах теоретически, поэтому только индекс.
    """

    __tablename__ = "paint_weld_pairs"
    __table_args__ = (
        UniqueConstraint("painted_item_id", name="ux_paint_weld_pairs_painted"),
        CheckConstraint("source IN ('auto', 'manual')", name="ck_paint_weld_pairs_source"),
    )

    id = Column(Integer, primary_key=True, index=True)
    painted_item_id = Column(
        Integer, ForeignKey("items.item_id"), nullable=False
    )
    welded_item_id = Column(
        Integer, ForeignKey("items.item_id"), nullable=False, index=True
    )
    source = Column(String(10), nullable=False, default="auto", server_default="auto")
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    painted_item = relationship("Item", foreign_keys=[painted_item_id])
    welded_item = relationship("Item", foreign_keys=[welded_item_id])


class PaintWeldChainLink(Base):
    """
    Локальная связь «окрасочный → сварочный» заказ (цепочка открытия, этап 2).

    В 1С сварочный документ несёт штатное основание
    (`ЗаказНаПроизводствоОснование_Key` + `ДокументОснование`/`_Type`,
    см. .docs/odata.md). Локальная запись — источник истины на стороне
    PRODPLAN и якорь идемпотентности повторного открытия цепочки.

    Один окрасочный заказ = один сварочный (painted_order_id UNIQUE) — это якорь
    идемпотентности повторного открытия цепочки.
    """

    __tablename__ = "paint_weld_chain_links"
    __table_args__ = (
        UniqueConstraint("painted_order_id", name="ux_paint_weld_chain_links_painted_order"),
    )

    id = Column(Integer, primary_key=True, index=True)
    painted_order_id = Column(
        Integer, ForeignKey("production_orders.order_id"), nullable=False
    )
    welded_order_id = Column(
        Integer, ForeignKey("production_orders.order_id"), nullable=False, index=True
    )
    pair_id = Column(Integer, ForeignKey("paint_weld_pairs.id"), nullable=False)
    created_at = Column(TIMESTAMP, default=func.now(), server_default=func.now(), nullable=False)
