"""Deterministic field retrieval and fusion with real embedding rankings."""
import asyncio
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

MAX_PAGE_BYTES = 1024 * 1024


def normalize(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def query_text(value):
    value = value.strip()
    return "" if value == "*" else value


def tokens(value):
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    result = re.findall(r"[a-z0-9]+", normalize(value))
    for word in re.findall(r"[\u3400-\u9fff]+", value):
        result.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
    return result


def schema_text(schema):
    """Only descriptive schema fields, not example/default argument values."""
    pieces = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {"description", "title"} and isinstance(value, str):
                pieces.append(value)
            elif key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
                pieces.extend(value.keys())
                pieces.extend(schema_text(v) for v in value.values())
            elif key in {"items", "additionalProperties", "anyOf", "oneOf", "allOf", "prefixItems"}:
                pieces.append(schema_text(value))
    elif isinstance(schema, list):
        pieces.extend(schema_text(v) for v in schema)
    return " ".join(pieces)


def document(key, names, fields):
    return {"id": key, "names": [n for n in names if n],
            "fields": fields, "text": "\n".join(text for text, _ in fields if text)}


def lexical(documents, query):
    terms = set(tokens(query))
    if not terms:
        return {}
    counts = [Counter(tokens(" ".join(text for text, _ in d["fields"]))) for d in documents]
    df = {term: sum(term in count for count in counts) for term in terms}
    average = sum(sum(c.values()) for c in counts) / max(1, len(counts)) or 1
    scores = {}
    for doc, count in zip(documents, counts):
        score = 0.0
        for text, weight in doc["fields"]:
            field = Counter(tokens(text))
            for term in terms:
                frequency = field[term]
                fuzzy = 1.0
                if not frequency and len(term) >= 4:
                    similar = max((SequenceMatcher(None, term, word).ratio()
                                   for word in field if abs(len(word) - len(term)) <= 2), default=0)
                    if similar >= .78:
                        frequency, fuzzy = 1, similar * .65
                if frequency:
                    idf = math.log(1 + (len(documents) - df[term] + .5) / (df[term] + .5))
                    score += weight * fuzzy * idf * frequency * 2.2 / (
                        frequency + 1.2 * (.25 + .75 * sum(count.values()) / average))
            if normalize(query) in normalize(text):
                score += weight * 2
        if score > 0:
            scores[doc["id"]] = score
    return scores


async def rank(documents, query, embeddings, config, scope, *, authorize=None, lexical_cache=None):
    status = {"mode": "keyword", "semantic_status": "not_needed"}
    query = query_text(query)
    if not query:
        return [(d, {"match_type": "all"}) for d in documents], status
    exact = [d for d in documents if normalize(query) in {normalize(n) for n in d["names"]}]
    if exact:
        return [(d, {"match_type": "exact"}) for d in exact], status
    words = (await lexical_cache(documents, query) if lexical_cache is not None
             else await asyncio.to_thread(lexical, documents, query))
    vectors, status = await embeddings.scores(scope, [{"id": d["id"], "text": d["text"]}
                                                    for d in documents], query, config, authorize=authorize)
    vectors = {key: value for key, value in vectors.items() if value >= config.get("min_similarity", .5)}
    word_order = sorted(words, key=lambda key: (-words[key], key))
    vector_order = sorted(vectors, key=lambda key: (-vectors[key], key))
    # Reciprocal Rank Fusion avoids comparing cosine with unbounded lexical scores.
    scores = {}
    for order in (word_order, vector_order):
        for position, key in enumerate(order, 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + position)
    by_id = {d["id"]: d for d in documents}
    results = []
    for key in sorted(scores, key=lambda key: (-scores[key], key)):
        match = {"match_type": "hybrid" if key in words and key in vectors else
                 "keyword" if key in words else "semantic", "score": round(scores[key], 6)}
        if key in vectors:
            match["semantic_similarity"] = round(vectors[key], 6)
        results.append((by_id[key], match))
    return results, status


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def paginate(items, *, cursor, limit, version, key):
    """Compatibility paginator for the former items-only discovery budget."""
    from .paging import decode_cursor, encode_cursor
    from .runtime import GatewayError

    offset = decode_cursor(cursor, version, key)
    if cursor and offset >= len(items):
        raise GatewayError("invalid_cursor", "Cursor offset is outside the result")
    selected = []
    for item in items[offset:offset + limit]:
        candidate = [*selected, item]
        following = encode_cursor(offset + len(candidate), version, key) if (
            offset + len(candidate) < len(items)
        ) else None
        page = {
            "items": candidate,
            "total": len(items),
            "returned": len(candidate),
            "next_cursor": following,
            "catalog_version": version,
        }
        size = len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode())
        if size > MAX_PAGE_BYTES:
            if not selected:
                name = item.get("gateway_name", item.get("name", "catalog entry"))
                raise GatewayError(
                    "catalog_entry_too_large",
                    f"Complete catalog contract for {name} exceeds the page budget",
                )
            break
        selected = candidate
    following = encode_cursor(offset + len(selected), version, key) if (
        offset + len(selected) < len(items)
    ) else None
    return {
        "items": selected,
        "total": len(items),
        "returned": len(selected),
        "next_cursor": following,
        "catalog_version": version,
    }
