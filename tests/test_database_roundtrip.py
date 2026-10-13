"""Database migration copies bounded batches and verifies portable content."""
import pytest
from sqlalchemy import func, select

from mcp_manager.cli import migrate_database
from mcp_manager.config import Settings
from mcp_manager.database import ApiToken, Database, User
from mcp_manager.token_secrets import decrypt_token_secret, encrypt_token_secret


@pytest.mark.asyncio
async def test_sqlite_roundtrip_is_bounded_verified_and_preserves_secrets(tmp_path):
    source_url = f"sqlite:///{tmp_path}/source.sqlite"
    target_url = f"sqlite:///{tmp_path}/target.sqlite"
    config = Settings(
        data_dir=tmp_path / "data",
        database_url=source_url,
        secret_key="roundtrip-secret",
    )
    source = Database(config)
    await source.initialize()
    async with source.locked() as session:
        users = [
            User(
                username=f"用户-{index:02d}",
                email=f"user{index}@example.test",
                password_hash="hash",
                mcp_ids=["服务-一"],
            )
            for index in range(13)
        ]
        session.add_all(users)
        await session.flush()
        session.add(
            ApiToken(
                user_id=users[0].id,
                name="迁移令牌",
                prefix="mcpm_roundtrip",
                token_hash="a" * 64,
                token_secret=encrypt_token_secret(config, "mcpm_secret-value"),
            )
        )
    await source.close()

    report = await migrate_database(config, target_url, batch_size=5)
    assert report.verified is True
    assert report.max_batch_rows <= 5
    assert report.table_counts["users"] == 13
    assert report.source_preserved is True
    assert report.table_hashes["users"]

    target_config = Settings(
        data_dir=tmp_path / "data",
        database_url=target_url,
        secret_key=config.secret_key,
    )
    target = Database(target_config)
    await target.initialize()
    async with target.session() as session:
        token = await session.scalar(select(ApiToken).where(ApiToken.name == "迁移令牌"))
        assert decrypt_token_secret(target_config, token.token_secret) == "mcpm_secret-value"
        assert await session.scalar(select(func.count()).select_from(User)) == 13
    await target.close()


@pytest.mark.asyncio
async def test_nonempty_target_is_refused_without_changing_source(tmp_path):
    source_config = Settings(
        data_dir=tmp_path / "source-data",
        database_url=f"sqlite:///{tmp_path}/source.sqlite",
        secret_key="source-secret",
    )
    target_config = Settings(
        data_dir=tmp_path / "target-data",
        database_url=f"sqlite:///{tmp_path}/target.sqlite",
        secret_key="source-secret",
    )
    for config, username in ((source_config, "source-user"), (target_config, "target-user")):
        db = Database(config)
        await db.initialize()
        async with db.locked() as session:
            session.add(User(username=username, password_hash="hash"))
        await db.close()

    with pytest.raises(ValueError, match="not empty"):
        await migrate_database(source_config, target_config.database_url, batch_size=2)

    source = Database(source_config)
    await source.initialize()
    async with source.session() as session:
        assert list(await session.scalars(select(User.username))) == ["source-user"]
    await source.close()
