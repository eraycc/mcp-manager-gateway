"""Policies are primary-backed, apply immediately, and expose only aggregate storage state."""
import httpx

from mcp_manager.database import SystemSetting
from mcp_manager.storage_policy import MIB, STORAGE_POLICY_KEY


async def test_policy_api_applies_budget_and_memory_without_changing_log_retention(db_app):
    web, state = db_app.web, db_app.app.state
    base = "/api/v1/settings/storage/governance"
    response = await web.patch(base, json={
        "snapshot_memory_bytes": 0, "artifact_memory_bytes": 0,
        "cache_cas_bytes": 2 * MIB})
    assert response.status_code == 200, response.text
    policy = response.json()["policy"]
    assert policy["cache_cas_bytes"] == 2 * MIB
    assert state.catalog.artifacts.cache_bytes == 0
    assert state.gateway.discovery.snapshots.max_bytes == 0
    assert state.gateway.discovery.snapshots.bytes == 0
    async with state.db.session() as session:
        assert (await session.get(SystemSetting, STORAGE_POLICY_KEY)).value == policy
    assert state.catalog.artifacts.admission.policy() == policy
    settings = (await web.get("/api/v1/settings")).json()
    assert settings["log_retention_days"] == settings["audit_log_retention_days"] == 0
    status = (await web.get(base)).json()
    assert status["retention"]["calls"]["permanent"]
    assert status["retention"]["audit"]["permanent"]
    assert "domains" in status["files"] and "staging" in status
    assert str(state.config.data_dir) not in str(status)


async def test_governance_validation_and_fresh_administrator_boundary(db_app):
    web = db_app.web
    base = "/api/v1/settings/storage/governance"
    for invalid in ({"unknown": 1}, {"staging_slots": True},
                    {"wal_warn_bytes": 3 * MIB, "wal_stop_bytes": 2 * MIB}):
        assert (await web.patch(base, json=invalid)).status_code == 422
    async with httpx.AsyncClient(transport=httpx.ASGITransport(db_app.app), base_url="http://test") as guest:
        assert (await guest.get(base)).status_code == 401
        assert (await guest.post(base + "/collect")).status_code == 401
    await web.post("/api/v1/auth/register", json={"username": "viewer", "password": "password12345"})
    await web.post("/api/v1/auth/login", json={"username": "viewer", "password": "password12345"})
    web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
    assert (await web.patch(base, json={})).status_code == 403
    assert (await web.post(base + "/collect")).status_code == 403


async def test_governor_refreshes_primary_policy_for_each_worker(db_app):
    state = db_app.app.state
    async with state.db.locked() as session:
        session.add(SystemSetting(key=STORAGE_POLICY_KEY,
                                  value={"snapshot_memory_bytes": 0, "artifact_memory_bytes": 0}))
    await state.storage_governance.refresh()
    assert state.catalog.artifacts.admission.policy()["snapshot_memory_bytes"] == 0
    assert state.gateway.discovery.snapshots.max_bytes == 0
