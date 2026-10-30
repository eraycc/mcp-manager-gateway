"""Versioned migrations for disposable log projections, separate from the business DB."""


def migrate_audit_index(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS audit_schema_versions (version INTEGER PRIMARY KEY)")
    version = conn.execute("SELECT COALESCE(MAX(version),0) FROM audit_schema_versions").fetchone()[0]
    if version < 1:
        conn.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE audit_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL, action TEXT NOT NULL, category TEXT, actor_type TEXT,
                user_id TEXT, username TEXT, target_type TEXT, target_id TEXT, target_name TEXT,
                result TEXT, source TEXT, ip TEXT, request_id TEXT, summary TEXT,
                path TEXT NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL, scan_id TEXT
            );
            CREATE INDEX audit_time ON audit_events(timestamp DESC,event_id DESC);
            CREATE INDEX audit_action_time ON audit_events(action,timestamp);
            CREATE INDEX audit_user_time ON audit_events(user_id,timestamp);
            CREATE INDEX audit_target ON audit_events(target_type,target_id);
            CREATE TABLE audit_index_offsets (
                path TEXT PRIMARY KEY, offset INTEGER NOT NULL, mtime_ns INTEGER NOT NULL
            );
            CREATE TABLE audit_delete_targets (
                operation_id TEXT NOT NULL, event_id TEXT NOT NULL, path TEXT NOT NULL,
                PRIMARY KEY(operation_id,event_id)
            );
            INSERT INTO audit_schema_versions VALUES(1);
            COMMIT;
        """)
    if version < 2:
        conn.execute("ALTER TABLE audit_index_offsets ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT INTO audit_schema_versions VALUES(2)")
    conn.commit()


def migrate_call_index(conn):
    """Versioned call projections, isolated from audit/catalog table ownership."""
    conn.execute("CREATE TABLE IF NOT EXISTS call_schema_versions(version INTEGER PRIMARY KEY)")
    version = conn.execute("SELECT COALESCE(MAX(version),0) FROM call_schema_versions").fetchone()[0]
    if version < 1:
        conn.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, offset INTEGER NOT NULL,
                size INTEGER NOT NULL, timestamp TEXT, user_id TEXT, username TEXT,
                token_id TEXT, token_name TEXT, mcp_id TEXT, mcp_name TEXT,
                tool_name TEXT, status TEXT, source TEXT, duration_ms REAL);
            CREATE INDEX IF NOT EXISTS calls_user_time ON calls(user_id,timestamp);
            CREATE INDEX IF NOT EXISTS calls_token ON calls(token_id);
            CREATE INDEX IF NOT EXISTS calls_time ON calls(timestamp DESC,id DESC);
            CREATE TABLE IF NOT EXISTS index_offsets(path TEXT PRIMARY KEY,offset INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS payloads(
                log_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL,path TEXT NOT NULL,size INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS payload_artifact ON payloads(artifact_id);
            CREATE TABLE call_owners(call_id TEXT PRIMARY KEY,worker_id TEXT);
            CREATE TABLE call_source_versions(path TEXT PRIMARY KEY,generation TEXT NOT NULL);
            INSERT INTO call_schema_versions VALUES(1);
            COMMIT;
        """)
    conn.commit()
    return version < 1
