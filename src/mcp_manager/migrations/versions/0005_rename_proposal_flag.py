"""Rename api_tokens.enable_mcp_proposals -> enable_mcp_proposal.

The agent-facing tool was renamed from gateway_propose_mcp to
gateway_mcp_proposals; the token feature flag is renamed accordingly
(renamed as a column on api_tokens). Data is preserved.

Dialect-aware: MySQL (online and offline ``--sql``) requires an
explicit type on CHANGE COLUMN, so it is passed together with
``existing_type``.  SQLite does not support ``ALTER COLUMN TYPE`` or
``SET NOT NULL`` outside batch mode, so only a pure ``RENAME COLUMN``
is emitted there.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    dialect = op.get_context().dialect.name
    if dialect == "mysql":
        op.alter_column(
            "api_tokens",
            "enable_mcp_proposals",
            new_column_name="enable_mcp_proposal",
            existing_type=sa.Boolean(),
            type_=sa.Boolean(),
        )
    else:
        op.alter_column(
            "api_tokens",
            "enable_mcp_proposals",
            new_column_name="enable_mcp_proposal",
        )


def downgrade():
    dialect = op.get_context().dialect.name
    if dialect == "mysql":
        op.alter_column(
            "api_tokens",
            "enable_mcp_proposal",
            new_column_name="enable_mcp_proposals",
            existing_type=sa.Boolean(),
            type_=sa.Boolean(),
        )
    else:
        op.alter_column(
            "api_tokens",
            "enable_mcp_proposal",
            new_column_name="enable_mcp_proposals",
        )
