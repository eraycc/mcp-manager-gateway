"""Persist MCP proposal test outcomes."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "mcp_proposals",
        sa.Column("test_status", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column("mcp_proposals", sa.Column("test_result", sa.JSON(), nullable=True))
    op.add_column(
        "mcp_proposals",
        sa.Column("test_error", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "mcp_proposals",
        sa.Column("tested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("mcp_proposals", "tested_at")
    op.drop_column("mcp_proposals", "test_error")
    op.drop_column("mcp_proposals", "test_result")
    op.drop_column("mcp_proposals", "test_status")
