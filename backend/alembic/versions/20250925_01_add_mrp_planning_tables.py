"""add mrp planning tables

Revision ID: 20250925_01
Revises: 
Create Date: 2025-09-25 09:36:30

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '20250925_01'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 14. planning_config_versions
    op.create_table(
        'planning_config_versions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    )
    # unique(version)
    op.create_index('ux_planning_config_version', 'planning_config_versions', ['version'], unique=True)
    # unique active config (partial)
    op.create_index(
        'ux_planning_config_active',
        'planning_config_versions',
        ['is_active'],
        unique=True,
        postgresql_where=sa.text('is_active = TRUE')
    )
    op.create_index('idx_planning_config_created_at', 'planning_config_versions', ['created_at'])

    # 15. planning_run
    op.create_table(
        'planning_run',
        sa.Column('run_id', sa.Integer(), primary_key=True),
        sa.Column('started_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('finished_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('started_by', sa.String(length=100), nullable=True),
        sa.Column('horizon_days', sa.Integer(), nullable=True),
        sa.Column('use_weekly', sa.Boolean(), nullable=False, server_default=sa.text('TRUE')),
        sa.Column('config_version_id', sa.Integer(), sa.ForeignKey('planning_config_versions.id'), nullable=True),
        sa.Column('config_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('kpi', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index('idx_planning_run_status', 'planning_run', ['status'])
    op.create_index('idx_planning_run_started_at', 'planning_run', ['started_at'])
    # GIN indexes
    op.create_index('idx_planning_run_kpi_gin', 'planning_run', ['kpi'], postgresql_using='gin')
    op.create_index('idx_planning_run_warn_gin', 'planning_run', ['warnings'], postgresql_using='gin')

    # 16. planned_order
    op.create_table(
        'planned_order',
        sa.Column('order_id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('planning_run.run_id', ondelete='CASCADE'), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.item_id'), nullable=False),
        sa.Column('qty', sa.Numeric(15, 3), nullable=False),
        sa.Column('need_date', sa.Date(), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('finish_date', sa.Date(), nullable=True),
        sa.Column('route_ref', sa.String(length=255), nullable=True),
        sa.Column('priority_index', sa.Numeric(10, 4), nullable=True),
        sa.Column('bucket_type', sa.String(length=10), nullable=False),
        sa.Column('bucket_date', sa.Date(), nullable=False),
        sa.Column('demand_ref', sa.Text(), nullable=True),
        sa.Column('demand_date', sa.Date(), nullable=True),
        sa.CheckConstraint("bucket_type IN ('daily','weekly')", name='ck_planned_order_bucket_type'),
    )
    op.create_index('idx_planned_order_run', 'planned_order', ['run_id'])
    op.create_index('idx_planned_order_item', 'planned_order', ['item_id'])
    op.create_index('idx_planned_order_need_date', 'planned_order', ['need_date'])
    op.create_index('idx_planned_order_bucket', 'planned_order', ['bucket_type', 'bucket_date'])
    op.create_index('idx_planned_order_priority', 'planned_order', ['priority_index'])
    op.create_index('idx_planned_order_dates', 'planned_order', ['start_date', 'finish_date'])
    op.create_index('idx_planned_order_run_item', 'planned_order', ['run_id', 'item_id'])

    # 17. planned_order_stage
    op.create_table(
        'planned_order_stage',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('planning_run.run_id', ondelete='CASCADE'), nullable=False),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('planned_order.order_id', ondelete='CASCADE'), nullable=False),
        sa.Column('stage_id', sa.Integer(), sa.ForeignKey('production_stages.stage_id'), nullable=False),
        sa.Column('area_id', sa.Integer(), sa.ForeignKey('production_resources.resource_id'), nullable=True),
        sa.Column('bucket_type', sa.String(length=10), nullable=False),
        sa.Column('bucket_date', sa.Date(), nullable=False),
        sa.Column('hours', sa.Numeric(12, 3), nullable=False, server_default=sa.text('0.0')),
        sa.CheckConstraint("bucket_type IN ('daily','weekly')", name='ck_planned_order_stage_bucket_type'),
    )
    op.create_index('idx_pos_run_order', 'planned_order_stage', ['run_id', 'order_id'])
    op.create_index('idx_pos_stage_area', 'planned_order_stage', ['stage_id', 'area_id'])
    op.create_index('idx_pos_bucket', 'planned_order_stage', ['bucket_type', 'bucket_date'])
    op.create_index('idx_pos_area_bucket', 'planned_order_stage', ['area_id', 'bucket_type', 'bucket_date'])
    op.create_index('idx_pos_run_stage', 'planned_order_stage', ['run_id', 'stage_id'])

    # 18. planned_purchase
    op.create_table(
        'planned_purchase',
        sa.Column('purchase_id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('planning_run.run_id', ondelete='CASCADE'), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.item_id'), nullable=False),
        sa.Column('qty', sa.Numeric(15, 3), nullable=False),
        sa.Column('need_date', sa.Date(), nullable=False),
        sa.Column('order_date', sa.Date(), nullable=False),
        sa.Column('lead_time_days', sa.Integer(), nullable=False),
        sa.Column('priority_index', sa.Numeric(10, 4), nullable=True),
        sa.Column('bucket_type', sa.String(length=10), nullable=False),
        sa.Column('bucket_date', sa.Date(), nullable=False),
        sa.Column('supplier_ref1c', sa.String(length=255), nullable=True),
        sa.CheckConstraint("bucket_type IN ('daily','weekly')", name='ck_planned_purchase_bucket_type'),
    )
    op.create_index('idx_planned_purchase_run', 'planned_purchase', ['run_id'])
    op.create_index('idx_planned_purchase_item', 'planned_purchase', ['item_id'])
    op.create_index('idx_planned_purchase_need', 'planned_purchase', ['need_date'])
    op.create_index('idx_planned_purchase_order', 'planned_purchase', ['order_date'])
    op.create_index('idx_planned_purchase_bucket', 'planned_purchase', ['bucket_type', 'bucket_date'])
    op.create_index('idx_pp_item_need', 'planned_purchase', ['item_id', 'need_date'])
    op.create_index('idx_pp_item_order', 'planned_purchase', ['item_id', 'order_date'])

    # 19. capacity_load
    op.create_table(
        'capacity_load',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('planning_run.run_id', ondelete='CASCADE'), nullable=False),
        sa.Column('area_id', sa.Integer(), sa.ForeignKey('production_resources.resource_id'), nullable=False),
        sa.Column('bucket_type', sa.String(length=10), nullable=False),
        sa.Column('bucket_date', sa.Date(), nullable=False),
        sa.Column('hours_planned', sa.Numeric(12, 3), nullable=False, server_default=sa.text('0.0')),
        sa.Column('hours_available', sa.Numeric(12, 3), nullable=False, server_default=sa.text('0.0')),
        sa.Column('overload_hours', sa.Numeric(12, 3), nullable=False, server_default=sa.text('0.0')),
        sa.CheckConstraint("bucket_type IN ('daily','weekly')", name='ck_capacity_load_bucket_type'),
    )
    op.create_unique_constraint('ux_capacity_load', 'capacity_load', ['run_id', 'area_id', 'bucket_type', 'bucket_date'])
    op.create_index('idx_capacity_load_over', 'capacity_load', ['overload_hours'])

    # 20. pegging_link
    op.create_table(
        'pegging_link',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('planning_run.run_id', ondelete='CASCADE'), nullable=False),
        sa.Column('child_item_id', sa.Integer(), sa.ForeignKey('items.item_id'), nullable=False),
        sa.Column('parent_item_id', sa.Integer(), sa.ForeignKey('items.item_id'), nullable=True),
        sa.Column('demand_ref', sa.Text(), nullable=True),
        sa.Column('qty_contribution', sa.Numeric(15, 3), nullable=False),
        sa.Column('need_date', sa.Date(), nullable=True),
        sa.Column('parent_need_date', sa.Date(), nullable=True),
    )
    op.create_index('idx_pegging_run_child', 'pegging_link', ['run_id', 'child_item_id'])
    op.create_index('idx_pegging_run_parent', 'pegging_link', ['run_id', 'parent_item_id'])


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_index('idx_pegging_run_parent', table_name='pegging_link')
    op.drop_index('idx_pegging_run_child', table_name='pegging_link')
    op.drop_table('pegging_link')

    op.drop_index('idx_capacity_load_over', table_name='capacity_load')
    op.drop_constraint('ux_capacity_load', 'capacity_load', type_='unique')
    op.drop_table('capacity_load')

    op.drop_index('idx_planned_purchase_bucket', table_name='planned_purchase')
    op.drop_index('idx_planned_purchase_order', table_name='planned_purchase')
    op.drop_index('idx_planned_purchase_need', table_name='planned_purchase')
    op.drop_index('idx_planned_purchase_item', table_name='planned_purchase')
    op.drop_index('idx_planned_purchase_run', table_name='planned_purchase')
    op.drop_index('idx_pp_item_need', table_name='planned_purchase')
    op.drop_index('idx_pp_item_order', table_name='planned_purchase')
    op.drop_table('planned_purchase')

    op.drop_index('idx_pos_run_stage', table_name='planned_order_stage')
    op.drop_index('idx_pos_area_bucket', table_name='planned_order_stage')
    op.drop_index('idx_pos_bucket', table_name='planned_order_stage')
    op.drop_index('idx_pos_stage_area', table_name='planned_order_stage')
    op.drop_index('idx_pos_run_order', table_name='planned_order_stage')
    op.drop_table('planned_order_stage')

    op.drop_index('idx_planned_order_run_item', table_name='planned_order')
    op.drop_index('idx_planned_order_dates', table_name='planned_order')
    op.drop_index('idx_planned_order_priority', table_name='planned_order')
    op.drop_index('idx_planned_order_bucket', table_name='planned_order')
    op.drop_index('idx_planned_order_need_date', table_name='planned_order')
    op.drop_index('idx_planned_order_item', table_name='planned_order')
    op.drop_index('idx_planned_order_run', table_name='planned_order')
    op.drop_table('planned_order')

    op.drop_index('idx_planning_run_warn_gin', table_name='planning_run')
    op.drop_index('idx_planning_run_kpi_gin', table_name='planning_run')
    op.drop_index('idx_planning_run_started_at', table_name='planning_run')
    op.drop_index('idx_planning_run_status', table_name='planning_run')
    op.drop_table('planning_run')

    op.drop_index('idx_planning_config_created_at', table_name='planning_config_versions')
    op.drop_index('ux_planning_config_active', table_name='planning_config_versions')
    op.drop_index('ux_planning_config_version', table_name='planning_config_versions')
    op.drop_table('planning_config_versions')