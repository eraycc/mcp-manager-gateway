"""Password hashing is bounded, off-loop, and never authorizes stale identities."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from mcp_manager import identity
from mcp_manager.database import User


@pytest.mark.asyncio
async def test_password_pool_is_off_loop_bounded_and_rejects_excess(monkeypatch):
    pool = identity.PasswordWorkPool(max_workers=2, max_waiters=1)
    release = threading.Event()
    two_started = threading.Event()
    guard = threading.Lock()
    active = 0
    peak = 0
    threads = set()
    main_thread = threading.get_ident()

    def blocked_hash(value):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            threads.add(threading.get_ident())
            if active == 2:
                two_started.set()
        release.wait(5)
        with guard:
            active -= 1
        return "hashed:" + value

    monkeypatch.setattr(identity, "hasher", SimpleNamespace(hash=blocked_hash))
    first = asyncio.create_task(pool.hash("one"))
    second = asyncio.create_task(pool.hash("two"))
    assert await asyncio.to_thread(two_started.wait, 5)
    third = asyncio.create_task(pool.hash("three"))
    await asyncio.sleep(0)
    with pytest.raises(identity.PasswordPoolBusy):
        await pool.hash("four")
    release.set()
    assert await asyncio.gather(first, second, third) == [
        "hashed:one",
        "hashed:two",
        "hashed:three",
    ]
    assert peak == 2
    assert threads and main_thread not in threads
    pool.close()


@pytest.mark.asyncio
async def test_identity_rate_limiter_expires_and_caps_keys():
    current = [0.0]
    limiter = identity.IdentityRateLimiter(max_entries=2, clock=lambda: current[0])
    await limiter.record(("login", "a"), 10, 5)
    current[0] = 1.0
    await limiter.record(("login", "b"), 10, 5)
    await limiter.record(("login", "c"), 10, 5)
    assert len(limiter._entries) == 2
    assert ("login", "a") not in limiter._entries
    current[0] = 10.0
    await limiter.check(("login", "fresh"), 10, 5)
    assert not limiter._entries


@pytest.mark.asyncio
async def test_password_work_does_not_hold_identity_write_lock(db_app, monkeypatch):
    pool = db_app.app.state.password_pool
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_verify(_value, _encoded):
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(pool, "verify", blocked_verify)
    login = asyncio.create_task(
        db_app.web.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password12345"},
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    async with db_app.db.locked() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        user.disabled = True
        user.auth_version += 1
    release.set()

    response = await asyncio.wait_for(login, 2)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_failed_login_limit_returns_retry_after(db_app):
    db_app.app.state.config.login_failure_limit = 2
    payload = {"username": "admin", "password": "definitely-wrong"}
    assert (await db_app.web.post("/api/v1/auth/login", json=payload)).status_code == 401
    assert (await db_app.web.post("/api/v1/auth/login", json=payload)).status_code == 401
    limited = await db_app.web.post("/api/v1/auth/login", json=payload)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
