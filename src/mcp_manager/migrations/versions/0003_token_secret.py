"""Retain newly minted API token secrets as encrypted ciphertext."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # Historical hashes remain valid for authentication but cannot reveal their
    # original secret. New and rotated rows populate this nullable ciphertext.
    op.add_column("api_tokens", sa.Column("token_secret", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("api_tokens", "token_secret")
