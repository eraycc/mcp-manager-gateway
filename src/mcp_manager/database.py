"""Async database models and Alembic initialization."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import PACKAGE_ROOT, Settings


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(254), default="")
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="user")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_version: Mapped[int] = mapped_column(Integer, default=1)
    scope_mode: Mapped[str] = mapped_column(String(16), default="selected")
    mcp_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(24))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope_mode: Mapped[str] = mapped_column(String(16), default="selected")
    mcp_ids: Mapped[list] = mapped_column(JSON, default=list)
    discovery_mode: Mapped[str] = mapped_column(String(16), default="native")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class McpServer(Base):
    __tablename__ = "mcp_servers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    transport: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16), default="lazy")
    isolation: Mapped[str] = mapped_column(String(16), default="service")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    auth_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = create_async_engine(settings.database_url)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(connection, record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.close()
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self):
        # Alembic creates and records the schema through explicit versioned migrations.
        def migrate(connection):
            cfg = Config()
            cfg.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        async with self.engine.begin() as conn:
            await conn.run_sync(migrate)

    async def close(self):
        await self.engine.dispose()

    @asynccontextmanager
    async def locked(self):
        """Serialize identity invariants across workers using a preseeded DB mutex."""
        async with self.session() as session:
            if self.engine.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                await session.execute(select(SystemSetting).where(
                    SystemSetting.key == "_identity_lock").with_for_update())
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


async def get_setting(db: Database, key: str, default: Any = None) -> Any:
    async with db.session() as session:
        item = await session.get(SystemSetting, key)
        return item.value if item else default


async def set_setting(db: Database, key: str, value: Any) -> None:
    async with db.locked() as session:
        item = await session.get(SystemSetting, key)
        if item:
            item.value = value
        else:
            session.add(SystemSetting(key=key, value=value))
