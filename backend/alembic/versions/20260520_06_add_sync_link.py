"""add sync_link table for PRODPLAN <-> 1C document idempotency

Plan rule (.docs/one_c_export_from_prodplan.md, "Идемпотентность"):
> Нужна таблица связей, чтобы повторный запуск не создавал дубль.

Минимальные поля sync_link описаны в плане. Эта миграция вводит саму таблицу;
наполнение делается сервисами экспорта (production_order, stock_transfer,
manufacture, purchase_order).

Revision ID: 20260520_06
Revises: 20260520_05
Create Date: 2026-05-20 20:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_06"
down_revision = "20260520_05"
branch_labels = None
depends_on = None


def _has_table(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_table(inspector, "sync_link"):
        return

    op.create_table(
        "sync_link",
        sa.Column("link_id", sa.Integer(), nullable=False),
        sa.Column("source_system", sa.String(length=50), nullable=False, server_default="PRODPLAN"),
        sa.Column("source_doctype", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("target_system", sa.String(length=50), nullable=False, server_default="1C"),
        sa.Column("target_entity", sa.String(length=100), nullable=False),
        sa.Column("target_ref_key", sa.String(length=36), nullable=True),
        sa.Column("target_number", sa.String(length=50), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("link_id"),
        # One source doc has at most one link to a given target entity. Used
        # by export services to skip duplicates and resume after errors.
        sa.UniqueConstraint(
            "source_system",
            "source_doctype",
            "source_id",
            "target_entity",
            name="ux_sync_link_source_target",
        ),
    )
    op.create_index("ix_sync_link_target_ref_key", "sync_link", ["target_ref_key"])
    op.create_index("ix_sync_link_target_entity", "sync_link", ["target_entity"])
    op.create_index("ix_sync_link_status", "sync_link", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_table(inspector, "sync_link"):
        return
    op.drop_index("ix_sync_link_status", table_name="sync_link")
    op.drop_index("ix_sync_link_target_entity", table_name="sync_link")
    op.drop_index("ix_sync_link_target_ref_key", table_name="sync_link")
    op.drop_table("sync_link")
