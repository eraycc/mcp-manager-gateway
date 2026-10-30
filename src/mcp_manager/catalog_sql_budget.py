"""Conservative logical SQL accounting, independent from logs and free database pages."""
from sqlalchemy import text

from .runtime import GatewayError

# These are admission estimates including row/index overhead, not physical-file
# measurements. File/WAL/free-space statistics remain separate in governance.
TABLES = {
    "catalog_entries": (("1024+length(CAST({p}summary AS BLOB))+length(CAST({p}search_text AS BLOB))"
                         "+length(CAST({p}original_name AS BLOB))"), "generation_id"),
    "catalog_terms": ("256", "term_generation"),
    "catalog_words": ("256+length(CAST({p}term AS BLOB))", None),
    "embedding_vectors": ("512+length(CAST({p}owner AS BLOB))", "generation_id"),
    "catalog_generations": ("1024+length(CAST({p}details AS BLOB))+length(CAST({p}owner AS BLOB))", "id"),
    "catalog_manifests": ("512", None),
    "catalog_object_refs": ("160", None),
    "artifact_objects": ("192", None),
}


def migrate_sql_budget(connection):
    connection.exec_driver_sql("""CREATE TABLE catalog_sql_usage(
        name TEXT PRIMARY KEY,estimated_bytes INTEGER NOT NULL,rows_count INTEGER NOT NULL)""")
    connection.exec_driver_sql("""CREATE TABLE catalog_generation_usage(
        id TEXT PRIMARY KEY REFERENCES catalog_generations(id) ON DELETE CASCADE,
        estimated_bytes INTEGER NOT NULL)""")
    connection.exec_driver_sql("INSERT INTO catalog_generation_usage SELECT id,0 FROM catalog_generations")
    for name, (expression, generation) in TABLES.items():
        current = expression.format(p="")
        connection.exec_driver_sql(
            f"INSERT INTO catalog_sql_usage SELECT '{name}',COALESCE(sum({current}),0),count(*) FROM {name}")
        if generation is not None:
            key = "g.id" if generation == "term_generation" else "t." + generation
            join = (" JOIN catalog_generations g ON g.index_id=t.generation_id"
                    if generation == "term_generation" else "")
            # Aggregate each source once. A correlated aggregate makes upgrades
            # scan every posting for every generation at large installations.
            connection.exec_driver_sql(f"""UPDATE catalog_generation_usage AS u
                SET estimated_bytes=u.estimated_bytes+totals.bytes
                FROM (SELECT {key} AS identity,sum({expression.format(p='t.')}) AS bytes
                      FROM {name} t{join} GROUP BY {key}) AS totals
                WHERE u.id=totals.identity""")
        for action in ("INSERT", "DELETE", "UPDATE"):
            old = expression.format(p="OLD.")
            new = expression.format(p="NEW.")
            delta = new if action == "INSERT" else "-" + old if action == "DELETE" else f"({new})-({old})"
            rows = 1 if action == "INSERT" else -1 if action == "DELETE" else 0
            generation_sql = ""
            if generation is not None:
                prefix = "OLD." if action == "DELETE" else "NEW."
                key = (f"(SELECT id FROM catalog_generations WHERE index_id={prefix}generation_id)"
                       if generation == "term_generation" else prefix + generation)
                if name == "catalog_generations" and action == "INSERT":
                    generation_sql = f"INSERT INTO catalog_generation_usage VALUES(NEW.id,{new});"
                elif name != "catalog_generations" or action != "DELETE":
                    generation_sql = f"UPDATE catalog_generation_usage SET estimated_bytes=estimated_bytes+({delta}) WHERE id={key};"
            connection.exec_driver_sql(f"""CREATE TRIGGER meter_{name}_{action.lower()}
                AFTER {action} ON {name} BEGIN
                UPDATE catalog_sql_usage SET estimated_bytes=estimated_bytes+({delta}),rows_count=rows_count+({rows})
                WHERE name='{name}';
                {generation_sql} END""")


async def check_sql_budget(session, growth_bytes, *, retiring=False):
    if type(growth_bytes) is not int or growth_bytes < 0:
        raise ValueError("Invalid SQL growth estimate")
    used, quota = (await session.execute(text("""SELECT
        (SELECT COALESCE(sum(estimated_bytes),0) FROM catalog_sql_usage),
        json_extract(data,'$.catalog_sql_bytes') FROM storage_policy_snapshot WHERE id=1"""))).one()
    if used + growth_bytes > quota:
        raise GatewayError("index_capacity", "Catalog index budget reached; collect retired data or increase the index budget")
    if retiring:
        retired, quota = (await session.execute(text("""SELECT
            (SELECT COALESCE(sum(u.estimated_bytes),0) FROM catalog_generation_usage u
             WHERE NOT EXISTS(SELECT 1 FROM catalog_heads h
                 WHERE h.generation_id=u.id OR h.pending_id=u.id OR h.refresh_id=u.id)),
            json_extract(data,'$.retiring_sql_bytes') FROM storage_policy_snapshot WHERE id=1"""))).one()
        if retired >= quota:
            raise GatewayError("index_capacity", "Retired index backlog reached its budget; let cleanup finish before rebuilding")
