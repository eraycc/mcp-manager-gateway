"""A fresh derived database must use the covering, term-first posting index."""
from sqlalchemy import text


async def test_term_probe_uses_a_covering_lookup_without_statistics(dual_db):
    async with dual_db.derived.session() as session:
        columns = (await session.execute(text("PRAGMA index_info(ix_catalog_term_lookup)"))).all()
        assert [row[2] for row in columns] == ["term_id", "generation_id", "entry_id", "frequency"]
        rows = (await session.execute(text("""EXPLAIN QUERY PLAN
            SELECT entry_id,sum(frequency) FROM catalog_terms INDEXED BY ix_catalog_term_lookup
            WHERE term_id IN(1,2) AND generation_id IN(10,20)
            GROUP BY entry_id"""))).all()
        assert any("COVERING INDEX ix_catalog_term_lookup" in row[-1] for row in rows)


async def test_rank_lookup_does_not_probe_each_allowed_generation_for_every_hit(dual_db):
    from sqlalchemy import event

    from mcp_manager.catalog_retrieval import search_candidates

    plans = []
    engine = dual_db.derived.engine.sync_engine
    def capture(connection, cursor, statement, parameters, context, executemany):
        if "WITH hits AS MATERIALIZED" in statement and not statement.startswith("EXPLAIN"):
            rows = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
            plans.extend(row[-1] for row in rows)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert await search_candidates(dual_db.derived, ["missing-generation"], "weather") == []
        lookups = [plan for plan in plans if "SEARCH e USING COVERING INDEX ix_catalog_entry_rank" in plan]
        assert lookups and all("(index_id=?)" in plan and "generation_id=?" not in plan for plan in lookups)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
