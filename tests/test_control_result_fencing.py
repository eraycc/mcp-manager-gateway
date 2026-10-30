"""Logical completions are idempotent and old outcomes cannot undo new success."""
from mcp_manager.catalog_control import CatalogControl
from mcp_manager.catalog_store import scope_key
from mcp_manager.runtime import GatewayError


async def test_duplicate_failure_is_counted_once_and_survives_projection_loss(db_app):
    from test_consecutive_operation_failures import healthy_service
    row, _, _ = await healthy_service(db_app, "http")
    catalog = db_app.app.state.catalog
    scope = await catalog.index_scope(row, db_app.actor["id"])
    ticket = await catalog.begin_operation(scope)
    for _ in range(2):
        await catalog.record_operation_result(row, db_app.actor["id"], scope=scope, ticket=ticket,
            operation_id="same-operation", error=GatewayError("connection_error", "fixture"))
    async with db_app.db.session() as session:
        assert (await session.get(CatalogControl, scope_key(scope))).health["startup_failure_count"] == 1


async def test_old_failure_cannot_replace_new_complete_success(db_app):
    from test_consecutive_operation_failures import healthy_service
    row, _, _ = await healthy_service(db_app, "http")
    catalog = db_app.app.state.catalog
    scope = await catalog.index_scope(row, db_app.actor["id"])
    before = await catalog.begin_operation(scope)
    after = await catalog.begin_operation(scope)
    await catalog.record_operation_result(row, db_app.actor["id"], scope=scope, ticket=after,
                                         operation_id="new-success")
    await catalog.record_operation_result(row, db_app.actor["id"], scope=scope, ticket=before,
        operation_id="old-failure", error=GatewayError("connection_error", "obsolete"))
    async with db_app.db.session() as session:
        assert not (await session.get(CatalogControl, scope_key(scope))).health
