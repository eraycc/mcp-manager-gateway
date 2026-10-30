"""Windows/Linux command entry points."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal

from filelock import FileLock
from sqlalchemy import func, select

from .about import VERSION
from .config import Settings, default_home
from .database import Base, Database, SystemSetting
from .home_migration import initialize_home, migrate_home, reconcile_data
from .server import serve as run_server


@dataclass(frozen=True)
class MigrationReport:
    verified: bool
    table_counts: dict[str, int]
    table_hashes: dict[str, str]
    batch_size: int
    max_batch_rows: int
    source_preserved: bool
    recovery: str


def _portable_value(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _portable_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_portable_value(item) for item in value]
    return value


def _update_table_hash(digest, table, rows):
    names = [column.name for column in table.columns]
    for row in rows:
        value = {name: _portable_value(row.get(name)) for name in names}
        digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")


def _table_query(table):
    primary = list(table.primary_key.columns)
    return select(table).order_by(*primary)


def _without_runtime_lock(table, rows):
    if table.name != SystemSetting.__tablename__:
        return rows
    return [row for row in rows if row.get("key") not in {"_identity_lock", "_storage_layout"}]


async def _read_table_batches(connection, table, batch_size):
    offset = 0
    while True:
        rows = [
            dict(row)
            for row in (
                await connection.execute(_table_query(table).offset(offset).limit(batch_size))
            ).mappings()
        ]
        if not rows:
            return
        offset += len(rows)
        yield _without_runtime_lock(table, rows)


async def migrate_database(config, target_url, *, batch_size=250):
    if type(batch_size) is not int or not 1 <= batch_size <= 250:
        raise ValueError("Migration batch_size must be between 1 and 250")
    target_config = Settings(data_dir=config.data_dir, database_url=target_url, secret_key=config.secret_key)
    if target_config.database_url == config.database_url:
        raise ValueError("Source and target databases must differ")
    counts = {}
    hashes = {}
    max_batch_rows = 0
    with FileLock(str(config.data_dir / "runtime.lock"), timeout=0) as runtime_guard:
        source, target = Database(config), Database(target_config)
        try:
            await source.initialize(runtime_guard=runtime_guard)
            await target.initialize(runtime_guard=runtime_guard)
            async with source.engine.connect() as src, target.engine.begin() as dst:
                for table in (table for table in Base.metadata.sorted_tables if table.name != "_catalog_clock"):
                    query = select(func.count()).select_from(table)
                    if table.name == SystemSetting.__tablename__:
                        query = query.where(table.c.key.not_in(("_identity_lock", "_storage_layout")))
                    if await dst.scalar(query):
                        raise ValueError("Target database is not empty; migration refused")
                source_hashes = {}
                for table in (table for table in Base.metadata.sorted_tables if table.name != "_catalog_clock"):
                    digest = hashlib.sha256()
                    count = 0
                    async for rows in _read_table_batches(src, table, batch_size):
                        if not rows:
                            continue
                        max_batch_rows = max(max_batch_rows, len(rows))
                        _update_table_hash(digest, table, rows)
                        count += len(rows)
                        await dst.execute(table.insert(), rows)
                    counts[table.name] = count
                    source_hashes[table.name] = digest.hexdigest()
                for table in (table for table in Base.metadata.sorted_tables if table.name != "_catalog_clock"):
                    digest = hashlib.sha256()
                    count = 0
                    async for rows in _read_table_batches(dst, table, batch_size):
                        if not rows:
                            continue
                        _update_table_hash(digest, table, rows)
                        count += len(rows)
                    if count != counts[table.name] or digest.hexdigest() != source_hashes[table.name]:
                        raise RuntimeError(
                            f"Verification failed for {table.name}; clear the target and retry while preserving source DATA_DIR"
                        )
                    hashes[table.name] = digest.hexdigest()
        finally:
            await source.close()
            await target.close()
    return MigrationReport(
        verified=True,
        table_counts=counts,
        table_hashes=hashes,
        batch_size=batch_size,
        max_batch_rows=max_batch_rows,
        source_preserved=True,
        recovery="Preserve source DATABASE_URL, DATA_DIR and SECRET_KEY until target acceptance succeeds.",
    )


def main():
    parser = argparse.ArgumentParser(
        prog="mcp-manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="MCP management gateway. Equivalent commands:\n  mmg\n  mcp-manager\n  mcp-manager-gateway",
        epilog="All three commands support serve and stdio. Use -v / --version to show the installed version.")
    parser.add_argument("--home", help="User configuration directory (default: ~/.mcp-manager)")
    parser.add_argument("-v", "--version", action="version", version="%(prog)s " + VERSION)
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="Run the Web console and MCP gateway")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    bridge = commands.add_parser("stdio", help="Bridge local stdio to the running gateway",
        description="Local stdio bridge: mmg stdio, mcp-manager stdio, or mcp-manager-gateway stdio.",
        epilog="Authentication: --token or MCP_MANAGER_TOKEN. URL: --url or MCP_MANAGER_URL.")
    bridge.add_argument("--url", default=os.environ.get("MCP_MANAGER_URL", "http://127.0.0.1:8765"))
    bridge.add_argument("--token", default=os.environ.get("MCP_MANAGER_TOKEN", ""))
    commands.add_parser("upgrade", help="Apply database schema migrations")
    storage = commands.add_parser("storage", help="Inspect or migrate the local storage layout")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser("status", help="Show persisted storage layout and legacy table counts")
    storage_migrate = storage_commands.add_parser("migrate", help="Migrate once while all runtimes are stopped")
    storage_migrate.add_argument("--dry-run", action="store_true", help="Inspect only; do not migrate")
    commands.add_parser("rebuild-logs", help="Rebuild the separate JSONL query index")
    reconcile = commands.add_parser("reconcile-data", help="Backfill metadata and import legacy cache/job indexes")
    reconcile.add_argument("--batch-size", type=int, default=250)
    migrate = commands.add_parser("migrate-db", help="Copy configuration into an empty SQLite/MySQL database")
    migrate.add_argument("--target-database-url", required=True)
    home = commands.add_parser("migrate-home", help="Copy legacy data into an empty user directory")
    home.add_argument("--from-data", required=True)
    home.add_argument("--from-env")
    args = parser.parse_args()
    args.command = args.command or "serve"
    try:
        import json
        if args.command == "stdio":
            asyncio.run(run_stdio(args))
            return
        if args.command == "migrate-home":
            print(json.dumps(migrate_home(args.from_data, args.home or default_home(), args.from_env)))
            return
        if args.command == "serve":
            initialize_home(args.home or default_home())
        config = Settings(**({"home_dir": args.home} if args.home else {}))
        if args.command == "serve":
            from .app import create_app
            if getattr(args, 'host', None):
                config.host = args.host
            if getattr(args, 'port', None):
                config.port = args.port
            run_server(create_app(config), host=config.host, port=config.port)
        elif args.command == "migrate-db":
            print(json.dumps(asdict(asyncio.run(migrate_database(config, args.target_database_url))), ensure_ascii=False))
        elif args.command == "reconcile-data":
            print(json.dumps(asyncio.run(reconcile_data(config, batch_size=args.batch_size)), ensure_ascii=False))
        elif args.command == "storage":
            print(json.dumps(asyncio.run(storage_command(config, args.storage_command,
                dry_run=getattr(args, "dry_run", False))), ensure_ascii=False))
        elif args.command == "upgrade":
            asyncio.run(upgrade(config))
        elif args.command == "rebuild-logs":
            asyncio.run(rebuild(config))
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


async def run_stdio(args):
    from .bridge import run_bridge
    await run_bridge(args.url, args.token)


async def storage_command(config, action, *, dry_run=False):
    from .storage_migration import storage_status
    with FileLock(str(config.data_dir / "runtime.lock"), timeout=0) as runtime_guard:
        db = Database(config)
        try:
            if action == "migrate" and not dry_run:
                await db.initialize(allow_storage_migration=True, runtime_guard=runtime_guard)
            return await storage_status(db)
        finally:
            await db.close()


async def upgrade(config):
    with FileLock(str(config.data_dir / "runtime.lock"), timeout=0) as runtime_guard:
        db = Database(config)
        try:
            await db.initialize(runtime_guard=runtime_guard)
        finally:
            await db.close()


async def rebuild(config):
    from .logs import LogStore
    with FileLock(str(config.data_dir / "runtime.lock"), timeout=0):
        store = LogStore(config.data_dir)
        try:
            await store.audit("logs.rebuild", None, {"phase": "started"}, source="cli", result="pending")
            await store.rebuild()
            await store.audits.rebuild()
            await store.audit("logs.rebuild", None, {"phase": "completed"}, source="cli")
        finally:
            await store.close()


if __name__ == "__main__":
    main()
