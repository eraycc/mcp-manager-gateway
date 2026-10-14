"""Portable precomputed lexical retrieval for catalog generations."""
import asyncio
import hashlib
import string
from collections import Counter

from sqlalchemy import bindparam, desc, func, select

from .catalog_models import CatalogEntry, CatalogGeneration, CatalogTerm
from .database import McpServer
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
    hashes = [term_hash(term) for term in terms]
    score = func.sum(CatalogTerm.frequency).label("lexical_score")
    matched = func.count(func.distinct(CatalogTerm.term_hash)).label("matched_terms")
    candidates = (
        select(
            CatalogTerm.entry_id.label("entry_id"),
            score,
            matched,
        )
        .where(
            CatalogTerm.generation_id.in_(generations),
            CatalogTerm.term_hash.in_(bindparam("term_hashes", expanding=True)),
        )
        .group_by(CatalogTerm.entry_id)
        .subquery()
    )
    statement = (
        select(
            CatalogEntry.id,
            CatalogEntry.generation_id,
            CatalogGeneration.server_id,
            CatalogEntry.original_name,
            CatalogEntry.gateway_name,
            CatalogEntry.schema_hash,
            CatalogEntry.artifact_id,
            CatalogEntry.artifact_bytes,
            CatalogEntry.summary,
            CatalogEntry.search_text,
        )
        .join(candidates, candidates.c.entry_id == CatalogEntry.id)
        .join(CatalogGeneration, CatalogGeneration.id == CatalogEntry.generation_id)
        .join(McpServer, McpServer.id == CatalogGeneration.server_id)
        .where(
            CatalogEntry.generation_id.in_(generations),
            CatalogEntry.kind == "tools",
        )
        .order_by(
            desc(candidates.c.matched_terms),
            desc(candidates.c.lexical_score),
            McpServer.slug,
            CatalogEntry.sort_key,
            CatalogEntry.name_hash,
        )
        .limit(candidate_limit)
    )
    async with db.session() as session:
        rows = list((await session.execute(statement, {"term_hashes": hashes})).all())
        if rows:
            return rows
        # Cold-query fallback is bounded independently of directory size.
        alternatives = await asyncio.to_thread(typo_hashes, terms)
        if not alternatives:
            return []
        return list((await session.execute(statement, {"term_hashes": alternatives})).all())
