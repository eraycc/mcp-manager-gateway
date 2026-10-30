"""Deleted identities release cache roots; disabled live identities keep their directory."""
from sqlalchemy import select

from mcp_manager.catalog_control import CatalogControl
from mcp_manager.catalog_models import CatalogHead, CatalogManifest
from mcp_manager.database import McpServer, User, now
from mcp_manager.owner_cleanup import prune_orphan_scopes


async def personal_scope(db_app):
    created = await db_app.web.post("/api/v1/auth/register",
                                   json={"username": "catalog-owner", "password": "password12345"})
    assert created.status_code in (200, 201), created.text
    async with db_app.db.session() as session:
        owner = await session.scalar(select(User.id).where(User.username == "catalog-owner"))
    catalog = db_app.app.state.catalog
    row = await catalog.create({"name": "Owner catalog", "mode": "disabled", "transport": "stdio",
                                "isolation": "user", "config": {"command": "never-launched"}})
    async with db_app.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    row = await catalog.get(row.id)
    scope = await catalog.index_scope(row, owner)
    generation = await catalog.index.begin_refresh(scope)
    await catalog.index.stage_page(generation.id, "tools", [
        {"name": "private-tool", "inputSchema": {"type": "object"}}])
    await catalog.index.publish(generation.id, expected_scope=scope)
    return owner, row


async def test_deleted_user_releases_authoritative_control_head_and_manifest_roots(db_app):
    owner, _row = await personal_scope(db_app)
    assert (await db_app.web.delete("/api/v1/users/" + owner)).status_code == 200
    for _ in range(3):
        # A startup GC cycle may already own the single-leader guard.
        # Join it before explicitly exercising this integration path.
        async with db_app.app.state.storage_governor.guard:
            pass
        result = await db_app.app.state.storage_governor.run_once(seconds=2, row_budget=2000)
        assert result["status"] in {"completed", "inventory_rebuilding"}
    async with db_app.db.session() as session:
        assert not (await session.scalars(select(CatalogControl).where(CatalogControl.owner == owner))).all()
    async with db_app.db.derived.session() as session:
        assert not (await session.scalars(select(CatalogHead).where(CatalogHead.owner == owner))).all()
        assert not (await session.scalars(select(CatalogManifest).where(CatalogManifest.current.is_(True)))).all()


async def test_disabled_live_user_and_disabled_server_do_not_lose_current_cache(db_app):
    owner, row = await personal_scope(db_app)
    async with db_app.db.locked() as session:
        (await session.get(User, owner)).disabled = True
        (await session.get(McpServer, row.id)).mode = "disabled"
    for _ in range(3):
        await prune_orphan_scopes(db_app.app.state, at=now(), limit=100)
    async with db_app.db.derived.session() as session:
        assert (await session.scalars(select(CatalogHead).where(CatalogHead.owner == owner))).all()
    async with db_app.db.session() as session:
        assert (await session.scalars(select(CatalogControl).where(CatalogControl.owner == owner))).all()


async def test_deleted_service_parent_is_removed_after_bounded_generation_gc(db_app):
    from mcp_manager.derived_models import CatalogServer
    from mcp_manager.retention import run_storage_retention

    _owner, row = await personal_scope(db_app)
    state = db_app.app.state
    await state.catalog.delete(row.id)
    for _ in range(30):
        await prune_orphan_scopes(state, at=now(), limit=100)
        await run_storage_retention(state, at=now(), batch_limit=250)
    async with db_app.db.derived.session() as session:
        assert await session.get(CatalogServer, row.id) is None
