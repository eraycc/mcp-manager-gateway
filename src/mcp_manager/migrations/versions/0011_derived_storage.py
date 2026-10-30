"""Detach authoritative references and remove verified legacy cache tables."""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    if not op.get_context().config.attributes.get("storage_migration_verified"):
        raise RuntimeError("Run mmg storage migrate before upgrading the storage schema")
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "catalog_control" not in inspector.get_table_names():
        from mcp_manager.catalog_control import CatalogControl
        CatalogControl.__table__.create(connection)
    for name in ("job_results", "proposal_test_entries"):
        inspector = sa.inspect(connection)
        fks = [fk for fk in inspector.get_foreign_keys(name)
               if fk["referred_table"] == "artifact_objects"]
        if fks:
            with op.batch_alter_table(name, naming_convention={
                    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}) as batch:
                for fk in fks:
                    constraint = fk["name"] or f"fk_{name}_artifact_id_artifact_objects"
                    batch.drop_constraint(constraint, type_="foreignkey")
        indexes = {item["name"] for item in sa.inspect(connection).get_indexes(name)}
        index = f"ix_{name}_artifact_id"
        if index not in indexes:
            op.create_index(index, name, ["artifact_id"])
    evidence = op.get_context().config.attributes.get("storage_controls", {})
    if evidence:
        import hashlib
        import json
        from datetime import datetime
        from pathlib import Path

        from mcp_manager.catalog_control import CatalogControl
        root = Path(op.get_context().config.attributes["storage_data_dir"]).resolve()
        path = (root / evidence["path"]).resolve()
        if not path.is_relative_to(root) or path.is_symlink():
            raise RuntimeError("Control evidence leaves the storage root")
        accumulator = hashlib.sha256()
        with path.open("rb") as stream:
            while raw := stream.read(65536):
                accumulator.update(raw)
        if accumulator.hexdigest() != evidence["sha256"]:
            raise RuntimeError("Control migration evidence integrity mismatch")
        with path.open("r", encoding="utf-8") as stream:
            pending = []
            for raw in stream:
                row = json.loads(raw)
                row["updated_at"] = datetime.fromisoformat(row["updated_at"])
                pending.append(row)
                if len(pending) == 250:
                    existing = set(connection.scalars(sa.select(CatalogControl.scope_key).where(
                        CatalogControl.scope_key.in_([item["scope_key"] for item in pending]))).all())
                    values = [item for item in pending if item["scope_key"] not in existing]
                    if values:
                        connection.execute(CatalogControl.__table__.insert(), values)
                    pending.clear()
            if pending:
                existing = set(connection.scalars(sa.select(CatalogControl.scope_key).where(
                    CatalogControl.scope_key.in_([item["scope_key"] for item in pending]))).all())
                values = [item for item in pending if item["scope_key"] not in existing]
                if values:
                    connection.execute(CatalogControl.__table__.insert(), values)
    for name in ("embedding_vectors", "catalog_terms", "catalog_entries",
                 "catalog_heads", "catalog_generations", "artifact_objects"):
        if name in sa.inspect(connection).get_table_names():
            op.drop_table(name)


def downgrade():
    raise RuntimeError("Restore a verified storage backup; cache schema cannot reconstruct durable history")
