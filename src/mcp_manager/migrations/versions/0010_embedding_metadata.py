"""Persist generation-bound embedding metadata and batch artifact references."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def timestamp():
    return sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "embedding_vectors",
        sa.Column(
            "generation_id",
            sa.String(36),
            sa.ForeignKey("catalog_generations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "entry_id",
            sa.String(36),
            sa.ForeignKey("catalog_entries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model_hash", sa.String(64), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), sa.ForeignKey("artifact_objects.id"), nullable=False),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False),
        sa.Column("vector_index", sa.Integer(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("created_at", timestamp(), nullable=False),
    )
    op.create_index(
        "ix_embedding_generation_model",
        "embedding_vectors",
        ["generation_id", "model_hash"],
    )
    op.create_index(
        "ix_embedding_scope_model",
        "embedding_vectors",
        ["scope_key", "model_hash"],
    )


def downgrade():
    op.drop_table("embedding_vectors")
