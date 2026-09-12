"""Windows/Linux command entry points."""
import argparse
import asyncio
import os
import sys

import uvicorn
from filelock import FileLock
from sqlalchemy import func, select

from .about import VERSION
from .config import Settings, default_home
from .home_migration import initialize_home, migrate_home
from .database import Base, Database, SystemSetting


async def migrate_database(config, target_url):
    target_config = Settings(data_dir=config.data_dir, database_url=target_url, secret_key=config.secret_key)
    if target_config.database_url == config.database_url:
        raise ValueError("Source and target databases must differ")
    with FileLock(str(config.data_dir / "runtime.lock"), timeout=0):
        source, target = Database(config), Database(target_config)
        try:
            await source.initialize()
            await target.initialize()
            async with source.engine.connect() as src, target.engine.begin() as dst:
                for table in Base.metadata.sorted_tables:
                    query = select(func.count()).select_from(table)
                    if table.name == SystemSetting.__tablename__:
                        query = query.where(table.c.key != "_identity_lock")
                    if await dst.scalar(query):
                        raise ValueError("Target database is not empty; migration refused")
                for table in Base.metadata.sorted_tables:
                    rows = (await src.execute(select(table))).mappings().all()
                    rows = [dict(r) for r in rows if not (table.name == "system_settings" and r["key"] == "_identity_lock")]
                    if rows:
                        await dst.execute(table.insert(), rows)
            print("Database copied and verified. Preserve DATA_DIR/SECRET_KEY and set DATABASE_URL to the target.")
        finally:
            await source.close()
            await target.close()


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
    commands.add_parser("rebuild-logs", help="Rebuild the separate JSONL query index")
    migrate = commands.add_parser("migrate-db", help="Copy configuration into an empty SQLite/MySQL database")
    migrate.add_argument("--target-database-url", required=True)
    home = commands.add_parser("migrate-home", help="Copy legacy data into an empty user directory")
    home.add_argument("--from-data", required=True)
    home.add_argument("--from-env")
    args = parser.parse_args()
    args.command = args.command or "serve"
    try:
        if args.command == "stdio":
            asyncio.run(run_stdio(args))
            return
        if args.command == "migrate-home":
            import json
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
            uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1)
        elif args.command == "migrate-db":
            asyncio.run(migrate_database(config, args.target_database_url))
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


async def upgrade(config):
    with FileLock(str(config.data_dir / "runtime.lock"), timeout=0):
        db = Database(config)
        try:
            await db.initialize()
        finally:
            await db.close()


async def rebuild(config):
    from .logs import LogStore
    with FileLock(str(config.data_dir / "runtime.lock"), timeout=0):
        store = LogStore(config.data_dir)
        await store.close()


if __name__ == "__main__":
    main()
