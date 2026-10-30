"""Portable precomputed lexical retrieval for catalog generations."""
import asyncio
import hashlib
import string
from collections import Counter

from sqlalchemy import BigInteger, Integer, String, desc, select, text

from .catalog_models import CatalogEntry
from .derived_queries import ALLOWED, HASHES, query_generations, query_hashes
from .tool_index import schema_text, tokens

MAX_CANDIDATES = 200
MAX_QUERY_TERMS = 16
MAX_DOCUMENT_TERMS = 512
MAX_FUZZY_HASHES = 4096


def term_hash(term: str) -> str:
    return hashlib.sha256(term.encode("utf-8")).hexdigest()


def index_document(generation: str, tool: dict) -> list[dict]:
    """Build bounded term-frequency rows once while a generation is staged."""
    source = " ".join(
        str(tool.get(key, "")) for key in ("name", "title", "description")
    )
    source += " " + schema_text(tool.get("inputSchema", {}))
    if not source.strip():
        source = tool.get("search_text", "")
    counts = Counter(tokens(source))
    selected = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_DOCUMENT_TERMS]
    return [
        {
            "generation_id": generation,
            "entry_id": tool["entry_id"],
            "term_hash": term_hash(term),
            "term": term,
            "frequency": frequency,
        }
        for term, frequency in selected
    ]


def typo_hashes(terms):
    """One-edit ASCII alternatives; probe the existing hash index, never scan terms."""
    output = set()
    for term in terms:
        if not 4 <= len(term) <= 32 or not term.isascii() or not term.isalpha():
            continue
        splits = [(term[:index], term[index:]) for index in range(len(term) + 1)]
        variants = (
            [left + right[1:] for left, right in splits if right]
            + [left + right[1] + right[0] + right[2:] for left, right in splits if len(right) > 1]
            + [left + char + right for left, right in splits for char in string.ascii_lowercase]
            + [left + char + right[1:] for left, right in splits if right for char in string.ascii_lowercase]
        )
        for value in variants:
            output.add(term_hash(value))
            if len(output) >= MAX_FUZZY_HASHES:
                return sorted(output)
    return sorted(output)


async def search_candidates(db, generations: list[str], query: str, *, limit: int = MAX_CANDIDATES):
    """Return at most 200 lightweight tool rows without tokenizing stored documents."""
    if not generations:
        return []
    terms = list(dict.fromkeys(tokens(query)))[:MAX_QUERY_TERMS]
    if not terms:
        return []
    candidate_limit = min(MAX_CANDIDATES, max(1, int(limit)))
    hashes = [bytes.fromhex(term_hash(term)) for term in terms]
    # The derived repository is SQLite. Pin the covering term-first access
    # path so new installations without ANALYZE do not scan every posting or
    # fetch one table page per matching frequency.
    # The scope check is residual: probing (entry, generation) for every
    # allowed generation would multiply each hit by the full directory size.
    ranked = text(f"""
        WITH hits AS MATERIALIZED(
            SELECT entry_id,sum(frequency) AS lexical_score,count(*) AS matched_terms
            FROM catalog_terms INDEXED BY ix_catalog_term_lookup
            WHERE term_id IN (SELECT id FROM catalog_words
                WHERE term_hash IN (SELECT hash FROM {HASHES.name}))
              AND generation_id IN (SELECT index_id FROM {ALLOWED.name})
            GROUP BY entry_id
        )
        SELECT e.index_id AS entry_index,g.server_id,h.lexical_score,h.matched_terms,
               s.slug AS server_slug,e.sort_key,e.name_hash
        FROM hits h
        CROSS JOIN catalog_entries e INDEXED BY ix_catalog_entry_rank ON e.index_id=h.entry_id
        CROSS JOIN catalog_generations g INDEXED BY ix_catalog_generation_rank ON g.id=e.generation_id
        CROSS JOIN catalog_servers s INDEXED BY ix_catalog_server_rank ON s.id=g.server_id
        WHERE CAST(e.generation_id AS TEXT) IN (SELECT id FROM {ALLOWED.name}) AND e.kind='tools'
        ORDER BY h.matched_terms DESC,h.lexical_score DESC,s.slug,e.sort_key,e.name_hash
        LIMIT :candidate_limit
    """).bindparams(candidate_limit=candidate_limit).columns(
        entry_index=BigInteger, server_id=String, lexical_score=Integer, matched_terms=Integer,
        server_slug=String, sort_key=String, name_hash=String).subquery()
    # Wide JSON/search text is fetched only for the final bounded candidate set.
    statement = (
        select(
            CatalogEntry.id, CatalogEntry.generation_id, ranked.c.server_id,
            CatalogEntry.original_name, CatalogEntry.gateway_name, CatalogEntry.schema_hash,
            CatalogEntry.artifact_id, CatalogEntry.artifact_bytes,
            CatalogEntry.summary, CatalogEntry.search_text,
        ).join(ranked, ranked.c.entry_index == CatalogEntry.index_id)
        .order_by(desc(ranked.c.matched_terms), desc(ranked.c.lexical_score),
                  ranked.c.server_slug, ranked.c.sort_key, ranked.c.name_hash)
    )
    async with query_generations(db, generations) as session:
        await query_hashes(session, hashes)
        rows = list((await session.execute(statement)).all())
        if rows:
            return rows
        # Cold-query fallback is bounded independently of directory size.
        alternatives = await asyncio.to_thread(typo_hashes, terms)
        if not alternatives:
            return []
        await query_hashes(session, [bytes.fromhex(value) for value in alternatives])
        return list((await session.execute(statement)).all())
