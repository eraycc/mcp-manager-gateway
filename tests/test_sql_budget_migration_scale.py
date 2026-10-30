"""Meter upgrades aggregate postings once, with a deterministic VM-work budget."""
import sqlite3

from mcp_manager.catalog_sql_budget import migrate_sql_budget


def test_meter_seed_does_not_scan_all_postings_for_every_generation():
    connection = sqlite3.connect(":memory:")
    class Adapter:
        exec_driver_sql = connection.execute
    try:
        for ddl in (
            "catalog_generations(id TEXT PRIMARY KEY,index_id INTEGER UNIQUE,owner TEXT,details TEXT)",
            "catalog_entries(generation_id TEXT,summary TEXT,search_text TEXT,original_name TEXT)",
            "catalog_terms(generation_id INTEGER,entry_id INTEGER,term_id INTEGER,PRIMARY KEY(generation_id,entry_id,term_id))",
            "catalog_words(term TEXT)",
            "embedding_vectors(generation_id TEXT,owner TEXT)",
            "catalog_manifests(id TEXT)", "catalog_object_refs(id TEXT)", "artifact_objects(id TEXT)",
        ):
            connection.execute("CREATE TABLE " + ddl)
        connection.executemany("INSERT INTO catalog_generations VALUES(?,?,?,?)",
                               [(str(index), index, "service", "{}") for index in range(100)])
        connection.executemany("INSERT INTO catalog_terms VALUES(?,?,?)",
                               [(generation, term, 1) for generation in range(100) for term in range(100)])
        steps = 0
        def budget():
            nonlocal steps
            steps += 1000
            return int(steps > 2000000)
        connection.set_progress_handler(budget, 1000)
        migrate_sql_budget(Adapter())
        connection.set_progress_handler(None, 0)
        assert connection.execute("SELECT estimated_bytes FROM catalog_sql_usage WHERE name='catalog_terms'").fetchone()[0] == 10000 * 256
        rows = connection.execute("SELECT estimated_bytes FROM catalog_generation_usage").fetchall()
        assert len(rows) == 100
        assert all(row[0] == 100 * 256 + 1024 + len("service") + len("{}") for row in rows)
    finally:
        connection.close()
