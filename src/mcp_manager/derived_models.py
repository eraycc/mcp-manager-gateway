"""Metadata owned only by the disposable derived index."""
import hashlib
from datetime import UTC, datetime

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class DerivedBase(DeclarativeBase):
    pass


def index_key(identity):
    """Compact stable internal key; UNIQUE constraints reject any collision."""
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big") & ((1 << 63) - 1)


def default_index_key(context):
    return index_key(context.get_current_parameters()["id"])


def utc_now():
    return datetime.now(UTC)


class CatalogServer(DerivedBase):
    """A descriptive projection, never an authorization source."""
    __tablename__ = "catalog_servers"
    __table_args__ = (Index("ix_catalog_server_rank", "id", "slug"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))
    isolation: Mapped[str] = mapped_column(String(16))
