"""Derived catalog projections and authoritative proposal evidence references."""
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, now, uid
from .db_types import UTCDateTime
from .derived_models import DerivedBase, default_index_key


class ArtifactObject(DerivedBase):
    __tablename__ = "artifact_objects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)


class CatalogGeneration(DerivedBase):
    __tablename__ = "catalog_generations"
    __table_args__ = (Index("ix_catalog_generation_rank", "id", "server_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    index_id: Mapped[int] = mapped_column(BigInteger, unique=True, default=default_index_key)
    control_epoch: Mapped[int] = mapped_column(Integer, default=0)
    refresh_fence: Mapped[int] = mapped_column(Integer, default=0)
    refresh_ticket: Mapped[str] = mapped_column(String(36), default="")
    pending_pages: Mapped[int] = mapped_column(Integer, default=0)
    scope_key: Mapped[str] = mapped_column(String(64), index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("catalog_servers.id", ondelete="CASCADE"), index=True)
    owner: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    credential_generation: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="staging", index=True)
    digest: Mapped[str] = mapped_column(String(64), default="")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class CatalogHead(DerivedBase):
    __tablename__ = "catalog_heads"
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("catalog_servers.id", ondelete="CASCADE"), index=True)
    owner: Mapped[str] = mapped_column(Text)
    generation_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    pending_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    refresh_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="empty")


class CatalogEntry(DerivedBase):
    __tablename__ = "catalog_entries"
    __table_args__ = (
        UniqueConstraint("generation_id", "kind", "name_hash", name="uq_catalog_entry_name"),
        Index("ix_catalog_entry_page", "generation_id", "kind", "sort_key", "name_hash"),
        Index("ix_catalog_entry_rank", "index_id", "generation_id", "kind", "sort_key", "name_hash"),
        Index("ix_catalog_entry_gateway", "gateway_hash"),
        Index("ix_catalog_entry_legacy", "legacy_gateway_hash"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    index_id: Mapped[int] = mapped_column(BigInteger, unique=True, default=default_index_key)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    original_name: Mapped[str] = mapped_column(Text)
    name_hash: Mapped[str] = mapped_column(String(64), index=True)
    sort_key: Mapped[str] = mapped_column(String(256))
    gateway_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    legacy_gateway_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_hash: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[dict] = mapped_column(JSON)
    search_text: Mapped[str] = mapped_column(Text)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact_objects.id"), index=True)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger)


class CatalogWord(DerivedBase):
    __tablename__ = "catalog_words"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    term: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now, index=True)


class CatalogTerm(DerivedBase):
    __tablename__ = "catalog_terms"
    __table_args__ = (
        Index("ix_catalog_term_lookup", "term_id", "generation_id", "entry_id", "frequency"),
        Index("ix_catalog_term_entry", "entry_id"),
    )
    generation_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_generations.index_id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_entries.index_id", ondelete="CASCADE"), primary_key=True
    )
    term_id: Mapped[int] = mapped_column(ForeignKey("catalog_words.id"), primary_key=True)
    frequency: Mapped[int] = mapped_column(Integer, default=1)


class ProposalTestEntry(Base):
    __tablename__ = "proposal_test_entries"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", "name_hash", name="uq_proposal_test_entry"),
        Index("ix_proposal_test_page", "run_id", "kind", "sort_key", "name_hash"),
        Index("ix_proposal_test_expiry", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("mcp_proposals.id", ondelete="CASCADE"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(Text)
    name_hash: Mapped[str] = mapped_column(String(64))
    sort_key: Mapped[str] = mapped_column(String(256))
    summary: Mapped[dict] = mapped_column(JSON)
    search_text: Mapped[str] = mapped_column(Text)
    artifact_id: Mapped[str] = mapped_column(String(64), index=True)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)


class CatalogManifest(DerivedBase):
    """Only current and two short-lived historical manifests retain object roots."""
    __tablename__ = "catalog_manifests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), index=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifact_objects.id"), nullable=True, index=True)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    current: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    retire_after: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class CatalogObjectRef(DerivedBase):
    __tablename__ = "catalog_object_refs"
    __table_args__ = (Index("ix_catalog_object_ref_object", "artifact_id", "manifest_id"),)
    manifest_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_manifests.id", ondelete="CASCADE"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact_objects.id"), primary_key=True)
