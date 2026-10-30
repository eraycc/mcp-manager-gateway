"""Authoritative, bounded owner-scoped operation control, without catalog data."""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, now
from .db_types import UTCDateTime


class CatalogControl(Base):
    __tablename__ = "catalog_control"
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True)
    owner: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    credential_generation: Mapped[str] = mapped_column(String(128))
    epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    refresh_fence: Mapped[int] = mapped_column(BigInteger, default=0)
    result_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    success_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    directory_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    health: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)


class CatalogDirectoryClock(Base):
    """A constant-size invalidation fence shared by every local worker."""
    __tablename__ = "_catalog_clock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, default=0)
