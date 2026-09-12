"""Record login origin metadata while preserving existing sessions."""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable columns preserve historical rows and older writers without
    # inventing a login origin or rebuilding the SQLite session table.
    op.add_column("auth_sessions", sa.Column("ip_address", sa.String(64), nullable=True))
    op.add_column("auth_sessions", sa.Column("user_agent", sa.String(1024), nullable=True))


def downgrade():
    op.drop_column("auth_sessions", "user_agent")
    op.drop_column("auth_sessions", "ip_address")
