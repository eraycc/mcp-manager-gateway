"""Versioned schemas of the single disposable derived database."""
from sqlalchemy import text

from .derived_models import DerivedBase


def migrate_derived_schema(connection):
    connection.execute(text("CREATE TABLE IF NOT EXISTS derived_schema_versions(version INTEGER PRIMARY KEY)"))
    version = connection.scalar(text("SELECT COALESCE(MAX(version),0) FROM derived_schema_versions"))
    if version < 1:
        # Explicit v1 owns all derived metadata; the main Alembic chain never
        # creates these models in the authority database.
        DerivedBase.metadata.create_all(connection)
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(1)"))
        version = 1
    if version == 1:
        from .catalog_models import CatalogManifest, CatalogObjectRef
        CatalogManifest.__table__.create(connection, checkfirst=True)
        CatalogObjectRef.__table__.create(connection, checkfirst=True)
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(2)"))
        version = 2
    if version == 2:
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS storage_migration_health(key TEXT PRIMARY KEY,revision INTEGER,health TEXT NOT NULL)")
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS storage_migration_owners(hash TEXT PRIMARY KEY,owner TEXT NOT NULL)")
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(3)"))
        version = 3
    if version == 3:
        connection.exec_driver_sql("""CREATE TABLE legacy_catalog_import(
            key TEXT PRIMARY KEY, revision INTEGER NOT NULL, source TEXT NOT NULL,
            offset_bytes INTEGER NOT NULL, size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL, priority INTEGER NOT NULL)""")
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(4)"))
        version = 4
    if version == 4:
        from uuid import uuid4
        connection.exec_driver_sql("CREATE TABLE derived_identity(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO derived_identity VALUES('repository',?)", (uuid4().hex,))
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(5)"))
        version = 5
    if version == 5:
        from .catalog_models import CatalogEntry
        for index in CatalogEntry.__table__.indexes:
            if index.name == "ix_catalog_entries_name_hash":
                index.create(connection, checkfirst=True)
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(6)"))
        version = 6
    if version == 6:
        from types import SimpleNamespace

        from .storage_admission import migrate_admission_schema
        migrate_admission_schema(SimpleNamespace(execute=connection.exec_driver_sql))
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(7)"))
        version = 7
    if version == 7:
        from .catalog_sql_budget import migrate_sql_budget
        migrate_sql_budget(connection)
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(8)"))
        version = 8
    if version == 8:
        from types import SimpleNamespace

        from .storage_admission import migrate_admission_schema
        migrate_admission_schema(SimpleNamespace(execute=connection.exec_driver_sql))
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(9)"))
        version = 9
    if version == 9:
        from .catalog_models import CatalogHead
        for index in CatalogHead.__table__.indexes:
            if index.name in {"ix_catalog_heads_generation_id", "ix_catalog_heads_pending_id", "ix_catalog_heads_refresh_id"}:
                index.create(connection, checkfirst=True)
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(10)"))
        version = 10
    if version == 10:
        from .catalog_models import CatalogTerm
        connection.exec_driver_sql("DROP INDEX IF EXISTS ix_catalog_term_lookup")
        for index in CatalogTerm.__table__.indexes:
            if index.name == "ix_catalog_term_lookup":
                index.create(connection)
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(11)"))
        version = 11
    if version == 11:
        from .catalog_models import CatalogEntry, CatalogGeneration
        from .derived_models import CatalogServer
        for model, name in ((CatalogEntry, "ix_catalog_entry_rank"),
                            (CatalogGeneration, "ix_catalog_generation_rank"),
                            (CatalogServer, "ix_catalog_server_rank")):
            for index in model.__table__.indexes:
                if index.name == name:
                    index.create(connection, checkfirst=True)
        connection.execute(text("INSERT INTO derived_schema_versions VALUES(12)"))
        version = 12
    if version != 12:
        raise RuntimeError("Unsupported derived index schema version")
    from types import SimpleNamespace

    from .cache_index_schema import migrate_cache_schema
    migrate_cache_schema(SimpleNamespace(execute=connection.exec_driver_sql))
