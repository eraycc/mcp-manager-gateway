"""Install a cross-worker directory fence covering direct SQL/config mutations."""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    from mcp_manager.catalog_control import CatalogDirectoryClock

    CatalogDirectoryClock.__table__.create(connection, checkfirst=True)
    if connection.scalar(sa.select(CatalogDirectoryClock.id).where(CatalogDirectoryClock.id == 1)) is None:
        connection.execute(CatalogDirectoryClock.__table__.insert().values(id=1, version=0))
    if "directory_sequence" not in {column["name"] for column in sa.inspect(connection).get_columns("catalog_control")}:
        op.add_column("catalog_control", sa.Column("directory_sequence", sa.BigInteger(),
                                                   nullable=False, server_default="0"))
    mysql = connection.dialect.name == "mysql"
    condition = (
        "NOT(NEW.revision <=> OLD.revision) OR NOT(NEW.credential_generation <=> OLD.credential_generation) "
        "OR NOT(NEW.epoch <=> OLD.epoch) OR NOT(NEW.directory_sequence <=> OLD.directory_sequence) "
        "OR NOT(NEW.health <=> OLD.health)"
        if mysql else
        "NEW.revision IS NOT OLD.revision OR NEW.credential_generation IS NOT OLD.credential_generation "
        "OR NEW.epoch IS NOT OLD.epoch OR NEW.directory_sequence IS NOT OLD.directory_sequence "
        "OR NEW.health IS NOT OLD.health"
    )
    for table in ("mcp_servers", "catalog_control", "system_settings"):
        for event in ("INSERT", "UPDATE", "DELETE"):
            name = "mmg_clock_" + table + "_" + event.lower()
            predicates = []
            if table == "catalog_control" and event == "UPDATE":
                predicates.append("(" + condition + ")")
            if table == "system_settings":
                sides = ("NEW", "OLD") if event == "UPDATE" else ("OLD",) if event == "DELETE" else ("NEW",)
                predicates.append("(" + " OR ".join(
                    f"({side}.`key` LIKE 'credential:%' OR {side}.`key` LIKE 'oauth:%')" for side in sides) + ")")
                if event == "UPDATE":
                    predicates.append("NOT(NEW.value <=> OLD.value)" if mysql else "NEW.value IS NOT OLD.value")
            when = " AND ".join(predicates)
            connection.exec_driver_sql("DROP TRIGGER IF EXISTS " + name)
            update = "UPDATE _catalog_clock SET version=version+1 WHERE id=1;"
            if mysql:
                body = "BEGIN " + ("IF " + when + " THEN " + update + " END IF;" if when else update) + " END"
                statement = f"CREATE TRIGGER {name} AFTER {event} ON {table} FOR EACH ROW " + body
            else:
                statement = f"CREATE TRIGGER {name} AFTER {event} ON {table} "
                statement += ("WHEN " + when + " ") if when else ""
                statement += "BEGIN " + update + " END"
            connection.execute(sa.text(statement))


def downgrade():
    connection = op.get_bind()
    for table in ("mcp_servers", "catalog_control", "system_settings"):
        for event in ("insert", "update", "delete"):
            connection.exec_driver_sql("DROP TRIGGER IF EXISTS mmg_clock_" + table + "_" + event)
    op.drop_table("_catalog_clock")
    op.drop_column("catalog_control", "directory_sequence")
