"""Main-database catalog metadata; full definitions live in immutable artifacts."""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, now, uid
from .db_types import UTCDateTime


class ArtifactObject(Base):
    __tablename__ = "artifact_objects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)


class CatalogGeneration(Base):
    __tablename__ = "catalog_generations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scope_key: Mapped[str] = mapped_column(String(64), index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True)
    owner: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    credential_generation: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="staging", index=True)
    digest: Mapped[str] = mapped_column(String(64), default="")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class CatalogHead(Base):
    __tablename__ = "catalog_heads"
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True)
    owner: Mapped[str] = mapped_column(Text)
    generation_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="SET NULL"), nullable=True
    )
    refresh_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="empty")


class CatalogEntry(Base):
    __tablename__ = "catalog_entries"
    __table_args__ = (
        UniqueConstraint("generation_id", "kind", "name_hash", name="uq_catalog_entry_name"),
        Index("ix_catalog_entry_page", "generation_id", "kind", "sort_key", "name_hash"),
        Index("ix_catalog_entry_gateway", "gateway_hash"),
        Index("ix_catalog_entry_legacy", "legacy_gateway_hash"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    original_name: Mapped[str] = mapped_column(Text)
    name_hash: Mapped[str] = mapped_column(String(64))
    sort_key: Mapped[str] = mapped_column(String(256))
    gateway_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    legacy_gateway_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_hash: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[dict] = mapped_column(JSON)
    search_text: Mapped[str] = mapped_column(Text)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact_objects.id"))
    artifact_bytes: Mapped[int] = mapped_column(BigInteger)


class CatalogTerm(Base):
    __tablename__ = "catalog_terms"
    __table_args__ = (Index("ix_catalog_term_lookup", "generation_id", "term_hash"),)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[str] = mapped_column(ForeignKey("catalog_entries.id", ondelete="CASCADE"), primary_key=True)
    term_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    term: Mapped[str] = mapped_column(Text)
    frequency: Mapped[int] = mapped_column(Integer, default=1)
