"""Physical SQLite reclamation never expires valid data or touches MySQL tables."""
import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from mcp_manager.storage_maintenance import (
    DEFAULT_POLICY,
    StorageMaintenance,
    eligible,
    sqlite_stats,
    vacuum_sqlite,
)


def fragmented(path):
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE keep (id INTEGER PRIMARY KEY, payload BLOB)")
        conn.executemany("INSERT INTO keep(payload) VALUES (?)", [(b"x" * 8192,)] * 96)
        conn.execute("DELETE FROM keep WHERE id != 1")
    return path


def test_thresholds_require_both_conditions():
    assert DEFAULT_POLICY == {"startup_enabled": True, "min_free_mib": 32, "min_free_percent": 20}
    assert eligible({"free_bytes": 32 * 1024**2, "free_percent": 20}, DEFAULT_POLICY)
    assert not eligible({"free_bytes": 31 * 1024**2, "free_percent": 99}, DEFAULT_POLICY)
    assert not eligible({"free_bytes": 64 * 1024**2, "free_percent": 19}, DEFAULT_POLICY)


def test_vacuum_reclaims_space_and_preserves_rows(tmp_path):
    path = fragmented(tmp_path / "数据库 空间.sqlite")
    before = sqlite_stats(path)
    assert before["free_bytes"] > 512 * 1024
    result = vacuum_sqlite(path)
    assert result["status"] == "completed"
    assert result["reclaimed_bytes"] > 512 * 1024
    assert result["after"]["free_bytes"] == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id, length(payload) FROM keep").fetchall() == [(1, 8192)]


def test_automatic_vacuum_checks_thresholds_before_writing(tmp_path):
    path = fragmented(tmp_path / "threshold.sqlite")
    before = sqlite_stats(path)
    assert vacuum_sqlite(path, policy=DEFAULT_POLICY)["status"] == "below_threshold"
    assert sqlite_stats(path)["free_bytes"] == before["free_bytes"]
    policy = DEFAULT_POLICY | {"min_free_mib": 0, "min_free_percent": 0}
    assert vacuum_sqlite(path, policy=policy)["status"] == "completed"


def test_busy_database_is_skipped_without_touching_transaction(tmp_path):
    path = fragmented(tmp_path / "busy.sqlite")
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO keep(payload) VALUES ('pending')")
        result = vacuum_sqlite(path)
        assert result["status"] == "busy"
        assert writer.in_transaction
        writer.rollback()


def test_missing_database_is_never_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        sqlite_stats(path)
    assert not path.exists()


def test_insufficient_disk_space_skips_vacuum(tmp_path, monkeypatch):
    path = fragmented(tmp_path / "space.sqlite")
    monkeypatch.setattr("mcp_manager.storage_maintenance.shutil.disk_usage",
                        lambda _: SimpleNamespace(free=0))
    assert vacuum_sqlite(path)["status"] == "insufficient_space"
    assert sqlite_stats(path)["free_bytes"] > 0


@pytest.mark.asyncio
async def test_manual_job_and_independent_policies(db_app):
    web, state = db_app.web, db_app.app.state
    response = await web.get("/api/v1/settings/storage")
    assert response.status_code == 200
    targets = response.json()["databases"]
    assert targets["logs"]["supported"]
    assert targets["logs"]["policy"] == DEFAULT_POLICY
    is_sqlite = state.db.engine.dialect.name == "sqlite"
    assert targets["main"]["supported"] is is_sqlite
    payload = {"logs": {"startup_enabled": False, "min_free_mib": 64, "min_free_percent": 30}}
    saved = await web.patch("/api/v1/settings/storage", json=payload)
    assert saved.status_code == 200
    assert saved.json()["databases"]["logs"]["policy"] == payload["logs"]
    assert saved.json()["databases"]["main"]["policy"] == (DEFAULT_POLICY if is_sqlite else None)
    if not is_sqlite:
        assert (await web.patch("/api/v1/settings/storage", json={"main": DEFAULT_POLICY})).status_code == 422
        assert (await web.post("/api/v1/settings/storage/main/vacuum")).status_code == 422
    target = "main" if is_sqlite else "logs"
    response = await web.post(f"/api/v1/settings/storage/{target}/vacuum")
    assert response.status_code == 200
    job_id = response.json()["id"]
    for _ in range(200):
        status = await state.jobs.store.get_status(job_id)
        if status["status"] not in {"queued", "running"}:
            break
        await asyncio.sleep(.02)
    assert status["status"] == "completed"
    result = await state.jobs.store.get_result(job_id, 0)
    assert result["ok"], result
    assert result["result"]["status"] == "completed"
    assert (await web.get("/api/v1/settings/storage")).json()["databases"]["logs"]["policy"] == payload["logs"]


@pytest.mark.asyncio
async def test_validation_and_permissions(db_app):
    import httpx

    web = db_app.web
    for invalid in [
        {"logs": {"min_free_percent": 101}}, {"logs": {"min_free_mib": -1}},
        {"logs": {"startup_enabled": 1}}, {"logs": {"unknown": 1}},
        {"logs": {"min_free_mib": True}}, {"unknown": {}},
    ]:
        assert (await web.patch("/api/v1/settings/storage", json=invalid)).status_code == 422
    assert (await web.post("/api/v1/settings/storage/unknown/vacuum")).status_code == 404
    async with httpx.AsyncClient(transport=httpx.ASGITransport(db_app.app), base_url="http://test") as anonymous:
        assert (await anonymous.get("/api/v1/settings/storage")).status_code == 401
    await web.post("/api/v1/auth/register", json={"username": "viewer", "password": "password12345"})
    await web.post("/api/v1/auth/login", json={"username": "viewer", "password": "password12345"})
    web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
    assert (await web.get("/api/v1/settings/storage")).status_code == 403
    assert (await web.post("/api/v1/settings/storage/logs/vacuum")).status_code == 403


@pytest.mark.asyncio
async def test_startup_once_and_no_hourly_compaction(db_app, monkeypatch):
    state = db_app.app.state
    calls = []
    async def run(target, *, automatic=False):
        calls.append((target, automatic))
        return {"status": "below_threshold"}
    monkeypatch.setattr(state.storage, "run", run)
    # The real lifespan already made its single startup pass.
    await state.storage.startup()
    assert calls == []
    from mcp_manager.app import _log_retention
    state.retention_next = 0
    await _log_retention(db_app.app)
    assert calls == []


@pytest.mark.asyncio
async def test_startup_honors_disabled_policy_and_runs_once(db_app, monkeypatch):
    state = db_app.app.state
    policy = {"startup_enabled": False, "min_free_mib": 0, "min_free_percent": 0}
    assert (await db_app.web.patch("/api/v1/settings/storage", json={"logs": policy})).status_code == 200
    maintenance = StorageMaintenance(state)
    calls = []
    def compact(target, path, policy):
        calls.append(target)
        return {"status": "completed", "reclaimed_bytes": 0}
    monkeypatch.setattr(maintenance, "_compact", compact)
    await maintenance.startup()
    await maintenance.startup()
    assert calls == (["main"] if state.db.engine.dialect.name == "sqlite" else [])


@pytest.mark.asyncio
async def test_same_database_compactions_never_overlap(db_app, monkeypatch):
    state = db_app.app.state
    entered, release = threading.Event(), threading.Event()
    def worker(*args, **kwargs):
        entered.set()
        release.wait(5)
        return {"status": "completed"}
    monkeypatch.setattr("mcp_manager.storage_maintenance.vacuum_sqlite", worker)
    task = asyncio.create_task(state.storage.run("logs"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert (await state.storage.run("logs"))["status"] == "busy"
    finally:
        release.set()
    assert (await task)["status"] == "completed"


@pytest.mark.asyncio
async def test_cancellation_waits_for_vacuum_worker(db_app, monkeypatch):
    state = db_app.app.state
    entered, release = threading.Event(), threading.Event()
    def worker(*args, **kwargs):
        entered.set()
        release.wait(5)
        return {"status": "completed"}
    monkeypatch.setattr("mcp_manager.storage_maintenance.vacuum_sqlite", worker)
    task = asyncio.create_task(state.storage.run("logs"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not state.storage.locks["logs"].locked()
