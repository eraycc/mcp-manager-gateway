"""Token feature switches and MCP proposal approval queue."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("api_tokens", sa.Column(
        "enable_resource_tools", sa.Boolean(), nullable=False, server_default=sa.false()
    ))
    op.add_column("api_tokens", sa.Column(
        "enable_mcp_proposals", sa.Boolean(), nullable=False, server_default=sa.false()
    ))
    op.create_table(
        "mcp_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", sa.String(36), sa.ForeignKey("api_tokens.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("declared_capabilities", sa.JSON(), nullable=False),
        sa.Column("requested_permissions", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=True),
        sa.Column("isolation", sa.String(16), nullable=True),
        sa.Column("config_isolation", sa.String(16), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=False),
        sa.Column("approved_mcp_id", sa.String(36), sa.ForeignKey("mcp_servers.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_proposals_status", "mcp_proposals", ["status"])
    op.create_index("ix_mcp_proposals_user_id", "mcp_proposals", ["user_id"])


def downgrade():
    op.drop_table("mcp_proposals")
    op.drop_column("api_tokens", "enable_mcp_proposals")
    op.drop_column("api_tokens", "enable_resource_tools")
