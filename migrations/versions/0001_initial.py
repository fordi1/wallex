"""Initial immutable event and runtime state tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decisions",
        sa.Column("decision_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), primary_key=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False, index=True),
        sa.Column("outcome", sa.String(32), nullable=False, index=True),
        sa.Column("reason_fa", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "order_intents",
        sa.Column("client_id", sa.String(64), primary_key=True),
        sa.Column("decision_id", sa.String(64), nullable=False, index=True),
        sa.Column("symbol", sa.String(32), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "orders",
        sa.Column("client_id", sa.String(64), primary_key=True),
        sa.Column("exchange_order_id", sa.String(64), index=True),
        sa.Column("symbol", sa.String(32), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "fills",
        sa.Column("fill_id", sa.String(128), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False, index=True),
        sa.Column("symbol", sa.String(32), nullable=False, index=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.String(64), nullable=False),
        sa.Column("price", sa.String(64), nullable=False),
        sa.Column("fee_tmn", sa.String(64), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "positions",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("quantity", sa.String(64), nullable=False),
        sa.Column("average_entry", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), index=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("equity_tmn", sa.String(64), nullable=False),
        sa.Column("drawdown", sa.String(64), nullable=False),
        sa.Column("daily_pnl", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "kill_switch_state",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "service_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "ai_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False, index=True),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("fallback", sa.Boolean(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "ai_calls",
        "service_state",
        "kill_switch_state",
        "equity_snapshots",
        "positions",
        "fills",
        "orders",
        "order_intents",
        "decisions",
    ):
        op.drop_table(table)
