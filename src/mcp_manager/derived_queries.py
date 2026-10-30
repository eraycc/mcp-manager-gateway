"""Connection-local allowed scopes keep SQLite parameter counts bounded."""
from contextlib import asynccontextmanager

from sqlalchemy import BigInteger, String, column, table, text

from .derived_models import index_key

ALLOWED = table("mmg_query_generations", column("id", String), column("index_id", BigInteger))
HASHES = table("mmg_query_hashes", column("hash"))


@asynccontextmanager
async def query_generations(db, identities):
    async with db.session() as session:
        await session.execute(text("""CREATE TEMP TABLE IF NOT EXISTS mmg_query_generations(
            id TEXT PRIMARY KEY,index_id INTEGER UNIQUE NOT NULL)"""))
        await session.execute(text("DELETE FROM mmg_query_generations"))
        identities = list(dict.fromkeys(identities))
        for offset in range(0, len(identities), 250):
            await session.execute(text("INSERT INTO mmg_query_generations VALUES(:id,:index_id)"),
                                  [{"id": identity, "index_id": index_key(identity)}
                                   for identity in identities[offset:offset + 250]])
        yield session
        # Session close rolls back only this connection's temporary writes.


async def query_hashes(session, hashes):
    await session.execute(text("CREATE TEMP TABLE IF NOT EXISTS mmg_query_hashes(hash BLOB PRIMARY KEY)"))
    await session.execute(text("DELETE FROM mmg_query_hashes"))
    for offset in range(0, len(hashes), 250):
        await session.execute(text("INSERT OR IGNORE INTO mmg_query_hashes VALUES(:hash)"),
                              [{"hash": value} for value in hashes[offset:offset + 250]])
