"""The primary change clock covers external SQL mutations without counting healthy calls."""
from sqlalchemy import select

from mcp_manager.catalog_control import CatalogControl, CatalogDirectoryClock
from mcp_manager.catalog_store import scope_key
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import McpServer, SystemSetting


async def version(database):
    async with database.session() as session:
        return await session.scalar(select(CatalogDirectoryClock.version).where(CatalogDirectoryClock.id == 1))


async def test_directory_clock_fences_config_credentials_and_publication(dual_db):
    before = await version(dual_db)
    async with dual_db.locked() as session:
        server = McpServer(name="clock", slug="clock", transport="stdio", config={})
        session.add(server)
    assert await version(dual_db) > before
    before = await version(dual_db)
    async with dual_db.locked() as session:
        session.add(SystemSetting(key=f"credential:{server.id}:service", value={"sealed": "fixture"}))
    assert await version(dual_db) > before
    scope = CatalogScope(server.id, "service", 1, "fixture")
    async with dual_db.locked() as session:
        session.add(CatalogControl(scope_key=scope_key(scope), server_id=server.id,
                                  owner="service", revision=1, credential_generation="fixture"))
    before = await version(dual_db)
    async with dual_db.locked() as session:
        control = await session.get(CatalogControl, scope_key(scope))
        control.result_sequence += 1
        control.success_sequence += 1
    assert await version(dual_db) == before, "Healthy calls cannot invalidate every search snapshot"
    async with dual_db.locked() as session:
        control = await session.get(CatalogControl, scope_key(scope))
        control.directory_sequence += 1
    assert await version(dual_db) > before
    before = await version(dual_db)
    async with dual_db.locked() as session:
        control = await session.get(CatalogControl, scope_key(scope))
        control.health = {"cache_error": "fixture"}
    assert await version(dual_db) > before
