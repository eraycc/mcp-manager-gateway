from sqlalchemy import text
from mcp_manager.config import Settings
from mcp_manager.database import Database, get_setting, set_setting


async def test_migration_and_setting_restart(tmp_path):
    settings=Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/db.sqlite")
    db=Database(settings)
    await db.initialize()
    await set_setting(db,"sample", {"nested":[1,2]})
    async with db.session() as s:
        assert (await s.execute(text("select version_num from alembic_version"))).scalar()
    await db.close()
    db=Database(settings)
    await db.initialize()
    assert await get_setting(db,"sample",None) == {"nested":[1,2]}
    await db.close()


def test_migration_offline_sql_for_sqlite_and_mysql(tmp_path, monkeypatch):
    from io import StringIO
    from alembic import command
    from alembic.config import Config
    from mcp_manager.config import PACKAGE_ROOT
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for url in ("sqlite:///example.db", "mysql://user:password@localhost/mcp"):
        monkeypatch.setenv("DATABASE_URL",url)
        output=StringIO()
        cfg=Config(output_buffer=output)
        cfg.set_main_option("script_location",str(PACKAGE_ROOT/"migrations"))
        command.upgrade(cfg,"head",sql=True)
        sql=output.getvalue()
        assert "CREATE TABLE users" in sql
        assert "_identity_lock" in sql


async def test_sqlite_foreign_keys_enabled(tmp_path):
    db=Database(Settings(data_dir=tmp_path))
    await db.initialize()
    async with db.session() as s:
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
    await db.close()
