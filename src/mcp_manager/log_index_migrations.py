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
    if version < 2:
        conn.executescript("""
            CREATE TABLE call_deletions(id TEXT PRIMARY KEY);
            CREATE TABLE call_event_sources(id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(id,path));
            CREATE INDEX call_event_sources_path ON call_event_sources(path,id);
            CREATE TABLE call_payload_roots(
                path TEXT NOT NULL,offset INTEGER NOT NULL,artifact_id TEXT NOT NULL,
                payload_path TEXT NOT NULL,size INTEGER NOT NULL,
                PRIMARY KEY(path,offset));
            CREATE INDEX call_payload_roots_object ON call_payload_roots(artifact_id);
            INSERT INTO call_schema_versions VALUES(2);
        """)
    if version < 3:
        conn.execute("ALTER TABLE call_payload_roots ADD COLUMN log_id TEXT")
        conn.execute("CREATE INDEX call_payload_roots_log ON call_payload_roots(log_id)")
        conn.execute("INSERT INTO call_schema_versions VALUES(3)")
    if version < 4:
        conn.execute("CREATE TABLE call_delete_requests(id TEXT PRIMARY KEY)")
        conn.execute("""CREATE TABLE call_delete_payload_candidates(
            artifact_id TEXT PRIMARY KEY,path TEXT NOT NULL)""")
        conn.execute("INSERT INTO call_schema_versions VALUES(4)")
    if version < 5:
        conn.execute("""CREATE TABLE call_rewrite_offsets(
            rewrite_id TEXT NOT NULL,old_offset INTEGER NOT NULL,new_offset INTEGER NOT NULL,
            PRIMARY KEY(rewrite_id,old_offset))""")
        conn.execute("INSERT INTO call_schema_versions VALUES(5)")
    if version < 6:
        conn.execute("CREATE TABLE call_deletion_sources(path TEXT PRIMARY KEY,inode TEXT NOT NULL,offset INTEGER NOT NULL)")
        conn.execute("INSERT INTO call_schema_versions VALUES(6)")
    from .storage_admission import migrate_admission_schema
    migrate_admission_schema(conn)
    conn.commit()
    return version < 3
