"""Compact postings use integer local identities and one copy of each word."""
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from .catalog_models import CatalogTerm, CatalogWord
from .derived_models import index_key
from .runtime import GatewayError


async def insert_term_rows(session, rows):
    if not rows:
        return
    words = {}
    for row in rows:
        identity = bytes.fromhex(row["term_hash"])
        if identity in words and words[identity] != row["term"]:
            raise GatewayError("catalog_hash_collision", "Catalog word hash collision")
        words[identity] = row["term"]
    resolved = {}
    hashes = list(words)
    for offset in range(0, len(hashes), 250):
        batch = hashes[offset:offset + 250]
        await session.execute(insert(CatalogWord).on_conflict_do_nothing(
            index_elements=["term_hash"]), [{"term_hash": key, "term": words[key]} for key in batch])
        for word in (await session.scalars(select(CatalogWord).where(CatalogWord.term_hash.in_(batch)))).all():
            if word.term != words[word.term_hash]:
                raise GatewayError("catalog_hash_collision", "Catalog word hash collision")
            resolved[word.term_hash] = word.id
    values = [{"generation_id": index_key(row["generation_id"]),
               "entry_id": index_key(row["entry_id"]), "term_id": resolved[bytes.fromhex(row["term_hash"])],
               "frequency": row["frequency"]} for row in rows]
    await session.execute(insert(CatalogTerm).on_conflict_do_nothing(), values)
