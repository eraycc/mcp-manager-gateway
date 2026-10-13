"""Separate fixed-size job status from append-only result references."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def timestamp():
    return sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table("jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        *[sa.Column(name, sa.Integer(), nullable=False) for name in
          ("total", "completed", "succeeded", "failed", "cancelled", "version")],
        sa.Column("error", sa.String(1024), nullable=False),
        sa.Column("created_at", timestamp(), nullable=False),
        sa.Column("updated_at", timestamp(), nullable=False))
    op.create_index("ix_jobs_owner_created", "jobs", ["user_id", "created_at"])
    op.create_index("ix_jobs_retention", "jobs", ["status", "updated_at"])
    op.create_table("job_results",
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("artifact_id", sa.String(64), sa.ForeignKey("artifact_objects.id"), nullable=False),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", timestamp(), nullable=False))


def downgrade():
    op.drop_table("job_results")
    op.drop_table("jobs")
