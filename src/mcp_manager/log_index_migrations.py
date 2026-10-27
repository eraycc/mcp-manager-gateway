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
    conn.commit()
