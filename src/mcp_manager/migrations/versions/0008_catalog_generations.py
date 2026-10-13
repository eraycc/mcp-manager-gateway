"""Portable catalog generations, lightweight summaries, indexes and object references."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def timestamp():
    return sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table("artifact_objects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", timestamp(), nullable=False))
    op.create_table("catalog_generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("server_id", sa.String(36), sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("credential_generation", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp(), nullable=False),
        sa.Column("published_at", timestamp(), nullable=True))
    for column in ("scope_key", "server_id", "status"):
        op.create_index("ix_catalog_generations_" + column, "catalog_generations", [column])
    op.create_table("catalog_heads",
        sa.Column("scope_key", sa.String(64), primary_key=True),
        sa.Column("server_id", sa.String(36), sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("generation_id", sa.String(36),
                  sa.ForeignKey("catalog_generations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("refresh_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=False))
    op.create_index("ix_catalog_heads_server_id", "catalog_heads", ["server_id"])
    op.create_table("catalog_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("generation_id", sa.String(36),
                  sa.ForeignKey("catalog_generations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("name_hash", sa.String(64), nullable=False),
        sa.Column("sort_key", sa.String(256), nullable=False),
        sa.Column("gateway_name", sa.String(64), nullable=True),
        sa.Column("gateway_hash", sa.String(64), nullable=True),
        sa.Column("legacy_gateway_hash", sa.String(64), nullable=True),
        sa.Column("schema_hash", sa.String(64), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("artifact_id", sa.String(64), sa.ForeignKey("artifact_objects.id"), nullable=False),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("generation_id", "kind", "name_hash", name="uq_catalog_entry_name"))
    op.create_index("ix_catalog_entries_generation_id", "catalog_entries", ["generation_id"])
    op.create_index("ix_catalog_entry_page", "catalog_entries", ["generation_id", "kind", "sort_key", "name_hash"])
    op.create_index("ix_catalog_entry_gateway", "catalog_entries", ["gateway_hash"])
    op.create_index("ix_catalog_entry_legacy", "catalog_entries", ["legacy_gateway_hash"])
    op.create_table("catalog_terms",
        sa.Column("generation_id", sa.String(36),
                  sa.ForeignKey("catalog_generations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("entry_id", sa.String(36), sa.ForeignKey("catalog_entries.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("term_hash", sa.String(64), primary_key=True),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("frequency", sa.Integer(), nullable=False))
    op.create_index("ix_catalog_term_lookup", "catalog_terms", ["generation_id", "term_hash"])


def downgrade():
    for name in ("catalog_terms", "catalog_entries", "catalog_heads", "catalog_generations", "artifact_objects"):
        op.drop_table(name)
