"""Bind approval tests to revisions and preserve exact identifiers and UTC precision."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TIMESTAMPS = {
    "users": {"created_at": False},
    "mcp_servers": {"created_at": False},
    "api_tokens": {"created_at": False, "expires_at": True},
    "auth_sessions": {"created_at": False, "expires_at": True},
    "mcp_proposals": {"created_at": False, "updated_at": False, "tested_at": True},
}
EXACT_FIELDS = (("users", "username", 64), ("mcp_servers", "slug", 128), ("api_tokens", "token_hash", 64))


def upgrade():
    op.add_column("mcp_proposals", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("mcp_proposals", sa.Column("test_run_id", sa.String(36), nullable=True))
    op.add_column("mcp_proposals", sa.Column("tested_revision", sa.Integer(), nullable=True))
    # Existing sealed payloads are not decrypted by DDL. Explicit reconciliation fills this column.
    op.add_column("mcp_proposals", sa.Column("public_name", sa.String(128), nullable=True))
    op.create_index("ix_mcp_proposals_status_created_id", "mcp_proposals", ["status", "created_at", "id"])
    if op.get_context().dialect.name == "mysql":
        for table, columns in TIMESTAMPS.items():
            for column, nullable in columns.items():
                op.alter_column(table, column, existing_type=sa.DateTime(), type_=DATETIME(fsp=6),
                                existing_nullable=nullable)
        for table, column, length in EXACT_FIELDS:
            op.alter_column(table, column, existing_type=sa.String(length),
                            type_=sa.String(length, collation="utf8mb4_bin"), existing_nullable=False)


def downgrade():
    op.drop_index("ix_mcp_proposals_status_created_id", table_name="mcp_proposals")
    # Precision/case guarantees are intentionally not weakened on downgrade.
    for column in ("public_name", "tested_revision", "test_run_id", "revision"):
        op.drop_column("mcp_proposals", column)
