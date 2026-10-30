"""Persistent metadata for generation-bound embedding vector batches."""
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import now
from .db_types import UTCDateTime
from .derived_models import DerivedBase


class EmbeddingVector(DerivedBase):
    __tablename__ = "embedding_vectors"
    __table_args__ = (
        Index("ix_embedding_generation_model", "generation_id", "model_hash"),
        Index("ix_embedding_scope_model", "scope_key", "model_hash"),
    )

    generation_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_generations.id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_entries.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    model_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact_objects.id"), index=True)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger)
    vector_index: Mapped[int] = mapped_column(Integer)
    dimension: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
