"""Versioned cache-body reference schema shared by all local repositories."""
CACHE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS cache_items (
       domain TEXT NOT NULL, key TEXT NOT NULL, artifact_id TEXT NOT NULL,
       size_bytes INTEGER NOT NULL, metadata TEXT NOT NULL,
       updated_at REAL NOT NULL, expires_at REAL,
       PRIMARY KEY(domain,key))""",
    "CREATE INDEX IF NOT EXISTS ix_cache_items_object ON cache_items(artifact_id)",
    "CREATE INDEX IF NOT EXISTS ix_cache_items_expiry ON cache_items(domain,expires_at,key)",
    "CREATE INDEX IF NOT EXISTS ix_cache_items_age ON cache_items(domain,updated_at,key)",
)


def migrate_cache_schema(connection):
    """This is schema version 1 of the cache-reference repository."""
    connection.execute("CREATE TABLE IF NOT EXISTS cache_schema_versions(version INTEGER PRIMARY KEY)")
    version = connection.execute("SELECT COALESCE(MAX(version),0) FROM cache_schema_versions").fetchone()[0]
    if version == 0:
        for statement in CACHE_SCHEMA:
            connection.execute(statement)
        connection.execute("INSERT INTO cache_schema_versions VALUES(1)")
        version = 1
    if version == 1:
        connection.execute("""CREATE TABLE cache_domain_usage(
            domain TEXT PRIMARY KEY, rows_count INTEGER NOT NULL, body_bytes INTEGER NOT NULL)""")
        connection.execute("""INSERT INTO cache_domain_usage SELECT domain,count(*),sum(size_bytes)
            FROM cache_items GROUP BY domain""")
        connection.execute("""CREATE TRIGGER cache_usage_insert AFTER INSERT ON cache_items BEGIN
            INSERT INTO cache_domain_usage VALUES(NEW.domain,1,NEW.size_bytes)
            ON CONFLICT(domain) DO UPDATE SET rows_count=rows_count+1,body_bytes=body_bytes+NEW.size_bytes;
            END""")
        connection.execute("""CREATE TRIGGER cache_usage_delete AFTER DELETE ON cache_items BEGIN
            UPDATE cache_domain_usage SET rows_count=rows_count-1,body_bytes=body_bytes-OLD.size_bytes
            WHERE domain=OLD.domain; END""")
        connection.execute("""CREATE TRIGGER cache_usage_update AFTER UPDATE OF size_bytes ON cache_items BEGIN
            UPDATE cache_domain_usage SET body_bytes=body_bytes+NEW.size_bytes-OLD.size_bytes
            WHERE domain=NEW.domain; END""")
        connection.execute("INSERT INTO cache_schema_versions VALUES(2)")
    elif version != 2:
        raise RuntimeError("Unsupported cache index schema version")
