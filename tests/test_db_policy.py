"""Catch timezone loss, non-finite JSON and missing durability/pool policies."""
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import StatementError

from mcp_manager.database import User, SystemSetting
from mcp_manager.oauth import OAuth


async def test_datetime_roundtrip_preserves_utc_and_microseconds(dual_db):
    stamp = datetime(2026, 10, 13, 12, 30, 20, 123456, tzinfo=UTC)
    async with dual_db.locked() as session:
        session.add(User(username="Case", password_hash="synthetic", created_at=stamp))
        session.add(User(username="case", password_hash="synthetic", created_at=stamp))
    async with dual_db.session() as session:
        from sqlalchemy import select
        rows = list((await session.scalars(select(User).order_by(User.username))).all())
        assert len(rows) == 2
        assert all(row.created_at.tzinfo is UTC for row in rows)
        assert all(row.created_at.microsecond == 123456 for row in rows)


async def test_nonfinite_json_is_rejected(dual_db):
    with pytest.raises((ValueError, StatementError)) as caught:
        async with dual_db.locked() as session:
            session.add(SystemSetting(key="invalid", value={"value": float("inf")}))
    # SQLAlchemy wraps the serialization exception; it must not be a DB/network failure.
    assert isinstance(getattr(caught.value, "orig", caught.value), ValueError)


async def test_connection_policy(dual_db):
    async with dual_db.session() as session:
        if dual_db.engine.dialect.name == "sqlite":
            assert (await session.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
            assert (await session.execute(text("PRAGMA synchronous"))).scalar_one() == 2
            assert (await session.execute(text("PRAGMA busy_timeout"))).scalar_one() == 10000
        else:
            assert dual_db.engine.pool.size() == 5
            assert dual_db.engine.pool._recycle == 1800


async def test_missing_oauth_expiry_is_json_null(monkeypatch):
    class Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"access_token": "synthetic-token"})
            ))
    monkeypatch.setattr("mcp_manager.oauth.httpx.AsyncClient", Client)
    value = await OAuth(None).exchange(
        {"token_url": "https://example.invalid/token"}, {"grant_type": "client_credentials"}
    )
    assert value["expires_at"] is None
