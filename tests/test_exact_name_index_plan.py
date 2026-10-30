"""All three exact name probes must use indexes, including a miss across many scopes."""
from sqlalchemy import text


async def test_original_name_hash_has_a_dedicated_lookup_index(dual_db):
    async with dual_db.derived.session() as session:
        plans = (await session.execute(text(
            "EXPLAIN QUERY PLAN SELECT id FROM catalog_entries WHERE name_hash=:hash"),
            {"hash": "0" * 64})).all()
    assert any("USING INDEX ix_catalog_entries_name_hash" in row[-1] for row in plans), plans


async def test_globally_missing_exact_name_does_not_stage_all_allowed_scopes(db_app, monkeypatch):
    from test_catalog_paged_api import indexed_catalog

    import mcp_manager.catalog_store as module

    row, _ = await indexed_catalog(db_app, 2)
    index = db_app.app.state.catalog.index
    scope = await db_app.app.state.catalog.index_scope(row)
    generation = (await index.get_summary(scope))["generation"]

    def forbidden(*args, **kwargs):
        raise AssertionError("An exact-name miss staged the complete allowed generation set")

    monkeypatch.setattr(module, "query_generations", forbidden)
    assert await index.query_tool_entries([generation], exact_names=["missing-name"], limit=0) == (0, [])


async def test_exact_result_page_starts_from_name_hits_before_allowed_generations(db_app):
    from sqlalchemy import event
    from test_catalog_paged_api import indexed_catalog

    row, tools = await indexed_catalog(db_app, 2)
    index = db_app.app.state.catalog.index
    scope = await db_app.app.state.catalog.index_scope(row)
    generation = (await index.get_summary(scope))["generation"]
    plans = []
    engine = db_app.db.derived.engine.sync_engine
    def capture(connection, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT catalog_entries.id,") and "ORDER BY catalog_servers.slug" in statement:
            plans.extend(item[-1] for item in connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + statement, parameters).all())
    event.listen(engine, "before_cursor_execute", capture)
    try:
        total, entries = await index.query_tool_entries(
            [generation, *("missing-" + str(i) for i in range(10000))],
            exact_names=[tools[0]["name"]], limit=20)
        assert total == 1 and entries[0].original_name == tools[0]["name"]
        lookups = [plan for plan in plans if plan.startswith("SEARCH catalog_entries USING")]
        assert lookups and all("generation_id=?" not in plan for plan in lookups), plans
        assert await index.query_tool_entries(["missing"], exact_names=[tools[0]["name"]]) == (0, [])
    finally:
        event.remove(engine, "before_cursor_execute", capture)


async def test_unique_exact_hit_stages_only_its_authorized_generation(db_app, monkeypatch):
    from test_catalog_paged_api import indexed_catalog

    import mcp_manager.catalog_store as module

    row, tools = await indexed_catalog(db_app, 2)
    index = db_app.app.state.catalog.index
    scope = await db_app.app.state.catalog.index_scope(row)
    generation = (await index.get_summary(scope))["generation"]
    original = module.query_generations
    staged = []
    def bounded(db, identities):
        identities = list(identities)
        staged.append(identities)
        assert identities == [generation]
        return original(db, identities)
    monkeypatch.setattr(module, "query_generations", bounded)
    total, entries = await index.query_tool_entries(
        [generation, *("missing-" + str(i) for i in range(10000))], exact_names=[tools[0]["name"]])
    assert total == 1 and entries[0].original_name == tools[0]["name"]
    assert staged == [[generation]]
    assert await index.query_tool_entries(["missing"], exact_names=[tools[0]["name"]]) == (0, [])
    assert staged == [[generation]], "Unauthorized global name hits must not stage or disclose any result"


async def test_large_exact_hit_set_keeps_all_generations_beyond_probe_limit(db_app):
    from uuid import uuid4

    from sqlalchemy import insert, select
    from test_catalog_paged_api import indexed_catalog

    from mcp_manager.catalog_models import CatalogEntry, CatalogGeneration

    _row, tools = await indexed_catalog(db_app, 1)
    index = db_app.app.state.catalog.index
    async with db_app.db.derived.session() as session:
        generation = (await session.execute(select(CatalogGeneration.__table__))).mappings().one()
        entry = (await session.execute(select(CatalogEntry.__table__))).mappings().one()
    generations = [str(uuid4()) for _ in range(251)]
    async with db_app.db.derived.locked() as session:
        await session.execute(insert(CatalogGeneration), [
            {**{key: value for key, value in generation.items() if key not in {"id", "index_id"}},
             "id": identity} for identity in generations])
        await session.execute(insert(CatalogEntry), [
            {**{key: value for key, value in entry.items() if key not in {"id", "index_id", "generation_id"}},
             "id": str(uuid4()), "generation_id": identity} for identity in generations])
    allowed = [generation["id"], *generations]
    total, entries = await index.query_tool_entries(allowed, exact_names=[tools[0]["name"]], limit=300)
    assert total == 252 and {item.generation_id for item in entries} == set(allowed)
    total, entries = await index.query_tool_entries([generations[-1]], exact_names=[tools[0]["name"]])
    assert total == 1 and entries[0].generation_id == generations[-1]
