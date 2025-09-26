"""seed initial planning_config_versions

Revision ID: 20250925_02
Revises: 20250925_01
Create Date: 2025-09-25 09:38:10

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from datetime import datetime


# revision identifiers, used by Alembic.
revision = '20250925_02'
down_revision = '20250925_01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Prepare initial config snapshot (aligned with .docs/progress.md policy spec)
    import json

    config_json = {
        "planning_horizon_days": 90,
        "mps_daily_horizon_days": 90,
        "weekly": {
            "enabled": True,
            "anchor_day": "Monday",
            "need_date_day": "Friday"
        },
        "procurement": {
            "default_lead_time_days": 30,
            "lead_time_min_policy": "max(default_lead_time_days, lead_time_from_item)",
            "lot_sizing": {
                "moq_source": "item_card_or_1",
                "multiple": 1,
                "rounding": "ceil"
            },
            "order_date_rounding_policy": "previous_workday"
        },
        "production": {
            "lot_sizing": {
                "min_batch": 1,
                "multiple": 1,
                "rounding": "ceil"
            }
        },
        "safety_stock_percent": 1,
        "capacity": {
            "use_resource_calendars": True,
            "consider_power_coefficients": True
        },
        "prioritization": {
            "weight_criticality": 0.4,
            "weight_importance": 0.3,
            "weight_cycle_time": 0.3,
            "default_importance": 1
        },
        "toggles": {
            "include_wip": False,
            "enable_weekly_route_detail": False
        }
    }

    conn = op.get_bind()

    # Determine next version number (fallback to 1)
    next_version = 1
    try:
        result = conn.execute(sa.text("SELECT COALESCE(MAX(version), 0) FROM planning_config_versions"))
        max_ver = result.scalar() or 0
        next_version = max_ver + 1 if max_ver >= 1 else 1
    except Exception:
        # If table is empty or any issue, default to version 1
        next_version = 1

    # Deactivate existing active version (if any)
    conn.execute(sa.text("UPDATE planning_config_versions SET is_active = FALSE WHERE is_active = TRUE"))

    # Insert initial (or next) active version with proper JSON binding (avoid :param::jsonb parsing issues)
    stmt = sa.text("""
        INSERT INTO planning_config_versions (version, is_active, config, comment, created_by, created_at)
        VALUES (:version, TRUE, CAST(:config AS JSONB), :comment, :created_by, CURRENT_TIMESTAMP)
    """).bindparams(sa.bindparam("config", type_=postgresql.JSONB()))

    conn.execute(
        stmt,
        {
            "version": next_version,
            "config": json.dumps(config_json, ensure_ascii=False),
            "comment": "initial planning config seed",
            "created_by": "alembic",
        }
    )


def downgrade() -> None:
    # Remove seeded config version(s) created by this migration
    # If multiple runs occurred, remove the record with comment='initial planning config seed'
    conn = op.get_bind()
    conn.execute(
        sa.text("""
            DELETE FROM planning_config_versions
            WHERE comment = :comment AND created_by = :created_by
        """),
        {"comment": "initial planning config seed", "created_by": "alembic"}
    )