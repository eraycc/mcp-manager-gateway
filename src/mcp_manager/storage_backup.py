"""Self-verifying offline logical backups with schema, rows, and a completion footer."""
import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text, tuple_
from sqlalchemy.schema import CreateIndex, CreateTable

from .db_types import strict_json_dumps
from .storage_async import run_blocking

MAX_BACKUP_RECORD = 64 * 1024 * 1024


def _value(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return {"base64_hex": value.hex()}
    if isinstance(value, Decimal):
        return str(value)
    return value


def verify_logical_backup(path):
    digest, counts, hashes, header, footer = hashlib.sha256(), {}, {}, None, None
    with path.open("rb") as source:
        while raw := source.readline(MAX_BACKUP_RECORD + 1):
            if len(raw) > MAX_BACKUP_RECORD or not raw.endswith(b"\n"):
                raise RuntimeError("Logical storage backup is incomplete")
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError("Logical storage backup is incomplete") from exc
            if item.get("kind") == "complete":
                footer = item
                if source.read(1):
                    raise RuntimeError("Unexpected data after storage backup footer")
                break
            digest.update(raw)
            if header is None:
                header = item
            elif "table" in item:
                name = item["table"]
                counts[name] = counts.get(name, 0) + 1
                hashes.setdefault(name, hashlib.sha256()).update(raw)
            else:
                raise RuntimeError("Invalid storage backup record")
    if not footer or not header:
        raise RuntimeError("Logical storage backup is incomplete")
    if header.get("format") != "mmg-main-backup" or header.get("version") != 1:
        raise RuntimeError("Unsupported storage backup format")
    if digest.hexdigest() != footer.get("sha256"):
        raise RuntimeError("Logical storage backup integrity mismatch")
    expected = {name: {"rows": counts.get(name, 0),
                       "sha256": hashes.get(name, hashlib.sha256()).hexdigest()}
                for name in header["tables"]}
    if footer.get("tables") != expected:
        raise RuntimeError("Logical storage backup row verification failed")
    return {"header": header, "tables": expected, "sha256": digest.hexdigest()}


async def write_logical_backup(engine, tables, destination, *, run_id):
    if destination.exists():
        evidence = await run_blocking(verify_logical_backup, destination)
        if evidence["header"]["run_id"] != run_id:
            raise RuntimeError("Storage backup belongs to a different migration")
        return evidence
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    metadata = next(iter(tables.values())).metadata
    ordered = [table for table in metadata.sorted_tables if table.name in tables]
    descriptors = {}
    for table in ordered:
        descriptors[table.name] = {
            "create": str(CreateTable(table).compile(dialect=engine.dialect)),
            "indexes": [str(CreateIndex(index).compile(dialect=engine.dialect))
                        for index in sorted(table.indexes, key=lambda index: index.name)],
            "columns": {column.name: column.type.__class__.__name__.lower() for column in table.columns},
        }
    async with engine.connect() as connection:
        if engine.dialect.name == "mysql":
            triggers = (await connection.execute(text(
                "SELECT TRIGGER_NAME,EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT "
                "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() LIMIT 257"))).all()
            if len(triggers) > 256:
                raise RuntimeError("Storage backup trigger inventory exceeds safety budget")
            trigger_sql = [
                f"CREATE TRIGGER `{row[0]}` {row[2]} {row[3]} ON `{row[1]}` FOR EACH ROW {row[4]}"
                for row in triggers if row[1] in tables]
        else:
            triggers = (await connection.execute(text(
                "SELECT tbl_name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name LIMIT 257"))).all()
            if len(triggers) > 256:
                raise RuntimeError("Storage backup trigger inventory exceeds safety budget")
            trigger_sql = [row[1] for row in triggers if row[0] in tables]
    header = {"format": "mmg-main-backup", "version": 1, "run_id": run_id,
              "dialect": engine.dialect.name, "created_at": datetime.now(UTC).isoformat(),
              "tables": descriptors, "triggers": trigger_sql}
    digest, evidence = hashlib.sha256(), {}

    def write(stream, item):
        raw = (strict_json_dumps(item) + "\n").encode()
        if len(raw) > MAX_BACKUP_RECORD:
            raise RuntimeError("Storage backup record exceeds safety budget")
        stream.write(raw)
        digest.update(raw)
        return raw

    try:
        with temporary.open("wb") as output:
            await run_blocking(write, output, header)
            async with engine.connect() as connection:
                # The runtime ownership barrier excludes writers; one repeatable
                # snapshot additionally covers every row table, including Alembic.
                if engine.dialect.name == "mysql":
                    connection = await connection.execution_options(isolation_level="REPEATABLE READ")
                async with connection.begin():
                    for table in ordered:
                        keys, previous, count, row_hash = list(table.primary_key.columns), None, 0, hashlib.sha256()
                        if not keys:
                            raise RuntimeError("Cannot verify a backup table without a primary key")
                        while True:
                            statement = select(table).order_by(*keys).limit(250)
                            if previous is not None:
                                statement = statement.where(tuple_(*keys) > tuple_(*previous))
                            rows = list((await connection.execute(statement)).mappings())
                            if not rows:
                                break
                            previous = tuple(rows[-1][column.name] for column in keys)
                            for row in rows:
                                raw = await run_blocking(write, output, {
                                    "table": table.name, "row": {key: _value(value) for key, value in row.items()}})
                                row_hash.update(raw)
                                count += 1
                        evidence[table.name] = {"rows": count, "sha256": row_hash.hexdigest()}
            footer = {"kind": "complete", "sha256": digest.hexdigest(), "tables": evidence}
            await run_blocking(output.write, (strict_json_dumps(footer) + "\n").encode())
            await run_blocking(output.flush)
            await run_blocking(os.fsync, output.fileno())
        await run_blocking(verify_logical_backup, temporary)
        await run_blocking(os.replace, temporary, destination)
        await run_blocking(destination.chmod, 0o600)
    finally:
        await run_blocking(temporary.unlink, missing_ok=True)
    return await run_blocking(verify_logical_backup, destination)


async def restore_logical_backup(engine, path):
    """Restore and verify a trusted backup into an explicitly empty offline database."""
    from sqlalchemy import DateTime, LargeBinary, MetaData, Numeric, inspect

    verified = await run_blocking(verify_logical_backup, path)
    header = verified["header"]
    if header["dialect"] != engine.dialect.name:
        raise ValueError("Storage backup restore requires the original database dialect")
    async with engine.begin() as connection:
        names = await connection.run_sync(lambda current: inspect(current).get_table_names())
        if names:
            raise ValueError("Storage backup restore requires an empty target")
        for descriptor in header["tables"].values():
            await connection.exec_driver_sql(descriptor["create"])
        for descriptor in header["tables"].values():
            for statement in descriptor["indexes"]:
                await connection.exec_driver_sql(statement)
        metadata = MetaData()
        await connection.run_sync(metadata.reflect)
        with path.open("rb") as source:
            source.readline(MAX_BACKUP_RECORD + 1)
            pending, name = [], None

            async def flush():
                if pending:
                    await connection.execute(metadata.tables[name].insert(), pending)
                    pending.clear()

            while raw := source.readline(MAX_BACKUP_RECORD + 1):
                record = json.loads(raw)
                if record.get("kind") == "complete":
                    break
                if record["table"] != name:
                    await flush()
                    name = record["table"]
                values = dict(record["row"])
                table = metadata.tables[name]
                if set(values) != set(table.columns.keys()):
                    raise RuntimeError("Storage backup columns disagree with schema")
                for column in table.columns:
                    value = values[column.name]
                    if value is None:
                        continue
                    if isinstance(column.type, DateTime):
                        values[column.name] = datetime.fromisoformat(value).replace(tzinfo=None)
                    elif isinstance(column.type, LargeBinary):
                        values[column.name] = bytes.fromhex(value["base64_hex"])
                    elif isinstance(column.type, Numeric):
                        values[column.name] = Decimal(value)
                pending.append(values)
                if len(pending) == 250:
                    await flush()
            await flush()
    # A successful INSERT is insufficient: verify every restored table independently.
    async with engine.connect() as connection:
        for name, table in metadata.tables.items():
            keys, previous, count, digest = list(table.primary_key.columns), None, 0, hashlib.sha256()
            while True:
                statement = select(table).order_by(*keys).limit(250)
                if previous is not None:
                    statement = statement.where(tuple_(*keys) > tuple_(*previous))
                rows = list((await connection.execute(statement)).mappings())
                if not rows:
                    break
                previous = tuple(rows[-1][column.name] for column in keys)
                for row in rows:
                    record = {"table": name, "row": {key: _value(value) for key, value in row.items()}}
                    digest.update((strict_json_dumps(record) + "\n").encode())
                    count += 1
            if verified["tables"][name] != {"rows": count, "sha256": digest.hexdigest()}:
                raise RuntimeError("Restored storage backup failed verification for " + name)
    async with engine.begin() as connection:
        for statement in header.get("triggers", []):
            await connection.execute(text(statement))
    return {"verified": True, "tables": verified["tables"], "sha256": verified["sha256"]}
