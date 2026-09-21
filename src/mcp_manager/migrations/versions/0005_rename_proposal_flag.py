"""Rename api_tokens.enable_mcp_proposals -> enable_mcp_proposal.

The agent-facing tool was renamed from gateway_propose_mcp to
gateway_mcp_proposals; the token feature flag is renamed accordingly
(renamed as a column on api_tokens). Data is preserved.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "api_tokens",
        "enable_mcp_proposals",
        new_column_name="enable_mcp_proposal",
    )


def downgrade():
    op.alter_column(
        "api_tokens",
        "enable_mcp_proposal",
        new_column_name="enable_mcp_proposals",
    )
