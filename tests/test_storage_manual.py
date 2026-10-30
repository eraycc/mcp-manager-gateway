"""Explicit cleanup keeps live catalog/durable roots and protects backup boundaries."""
from mcp_manager.database import SystemSetting
from mcp_manager.storage_async import run_blocking
from mcp_manager.storage_migration import LAYOUT_KEY


async def test_manual_query_cache_clear_keeps_primary_and_durable_objects(db_app):
    state, web = db_app.app.state, db_app.web
    await run_blocking(state.catalog.cache_store.set, "manual-test", {"value": "cached"})
    job = await state.jobs.store.create_job("manual-fixture", 1)
    await state.jobs.store.append_result(job["id"], 0, {"ok": True, "result": {"value": "persistent"}})
    primary = await web.get("/api/v1/mcps")
    response = await web.post("/api/v1/settings/storage/governance/query-cache/clear")
    await state.jobs.tasks[response.json()["id"]]
    assert await run_blocking(state.catalog.cache_store.get, "manual-test") is None
    assert (await state.jobs.store.get_result(job["id"], 0))["result"] == {"value": "persistent"}
    assert (await web.get("/api/v1/mcps")).json() == primary.json()


async def test_backup_delete_requires_completed_receipt_exact_confirmation_and_regular_tree(db_app):
    state, web = db_app.app.state, db_app.web
    identity = "a" * 32
    folder = state.config.data_dir / "backups/storage" / identity
    folder.mkdir(parents=True)
    (folder / "main.sqlite").write_bytes(b"backup")
    unrelated = state.config.data_dir / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    async with state.db.locked() as session:
        row = await session.get(SystemSetting, LAYOUT_KEY)
        row.value = {"version": 2, "phase": "completed", "run_id": identity}
    base = "/api/v1/settings/storage/governance/backup"
    assert (await web.get(base)).json()["backup"]["exists"]
    assert (await web.post(base + "/" + identity + "/delete", json={})).status_code == 422
    assert (await web.post(base + "/" + "b" * 32 + "/delete", json={"confirm": "b" * 32})).status_code == 409
    response = await web.post(base + "/" + identity + "/delete", json={"confirm": identity})
    assert response.status_code == 200, response.text
    await state.jobs.tasks[response.json()["id"]]
    assert not folder.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not (await web.get(base)).json()["backup"]["exists"]
    async with state.db.session() as session:
        assert (await session.get(SystemSetting, LAYOUT_KEY)).value["backup_deleted_at"]


async def test_pending_migration_backup_cannot_be_deleted(db_app):
    state, web = db_app.app.state, db_app.web
    identity = "c" * 32
    async with state.db.locked() as session:
        row = await session.get(SystemSetting, LAYOUT_KEY)
        row.value = {"phase": "failed", "run_id": identity}
    base = "/api/v1/settings/storage/governance/backup/" + identity + "/delete"
    assert (await web.post(base, json={"confirm": identity})).status_code == 409
