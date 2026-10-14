"""Bounded OpenAI-compatible embedding client with a scoped persistent cache."""
import asyncio
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from weakref import WeakValueDictionary

import httpx
from fastapi import HTTPException
from sqlalchemy import and_, delete, exists, insert, or_, select, tuple_
from sqlalchemy.orm import aliased

from .catalog_models import ArtifactObject, CatalogEntry, CatalogGeneration, CatalogHead
from .catalog_store import generation_scope, scope_key
from .catalog_types import ArtifactRef, CatalogScope
from .database import McpServer
from .embedding_models import EmbeddingVector
from .jsonl_store import JsonlStore
from .runtime import GatewayError

MAX_TEXT_CHARS = 16_000
MAX_DOCUMENTS = 1_000
MAX_BATCH_INPUTS = 32
MAX_BATCH_CHARS = 256_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PROBE_TEXT = "MCP semantic discovery probe"


class _EmbeddingError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _safe_endpoint(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise _EmbeddingError("invalid_config")
    try:
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        netloc = host + (f":{parsed.port}" if parsed.port is not None else "")
    except (ValueError, TypeError):
        raise _EmbeddingError("invalid_config") from None
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1/embeddings"
    elif not path.endswith("/embeddings"):
        path += "/embeddings"
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _settings(config: dict) -> tuple[str, str, str, float, float]:
    if not isinstance(config, dict):
        raise _EmbeddingError("invalid_config")
    endpoint = _safe_endpoint(config.get("base_url", ""))
    model = config.get("model", "")
    api_key = config.get("api_key", "")
    timeout = config.get("timeout_seconds", 10.0)
    minimum = config.get("min_similarity", 0.5)
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise _EmbeddingError("invalid_config")
    if not isinstance(api_key, str) or len(api_key) > 4096:
        raise _EmbeddingError("invalid_config")
    if not _number(timeout) or not 0 < float(timeout) <= 60:
        raise _EmbeddingError("invalid_config")
    if not _number(minimum) or not -1 <= float(minimum) <= 1:
        raise _EmbeddingError("invalid_config")
    return endpoint, model.strip(), api_key, float(timeout), float(minimum)


def _validated_vectors(payload, count: int) -> list[list[float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise _EmbeddingError("invalid_response")
    data = payload["data"]
    if len(data) != count:
        raise _EmbeddingError("invalid_response")
    ordered = [None] * count
    dimension = None
    for item in data:
        if (not isinstance(item, dict) or not isinstance(item.get("index"), int)
                or isinstance(item.get("index"), bool)):
            raise _EmbeddingError("invalid_response")
        position = item["index"]
        vector = item.get("embedding")
        if not 0 <= position < count or ordered[position] is not None or not isinstance(vector, list) or not vector:
            raise _EmbeddingError("invalid_response")
        if not all(_number(value) for value in vector):
            raise _EmbeddingError("invalid_response")
        normalized = [float(value) for value in vector]
        if not any(value != 0 for value in normalized):
            raise _EmbeddingError("invalid_response")
        if dimension is None:
            dimension = len(normalized)
        elif len(normalized) != dimension:
            raise _EmbeddingError("invalid_response")
        ordered[position] = normalized
    if any(vector is None for vector in ordered):
        raise _EmbeddingError("invalid_response")
    return ordered


def _valid_cached(value):
    vector = value.get("vector") if isinstance(value, dict) else None
    if not isinstance(vector, list) or not vector or not all(_number(item) for item in vector):
        return None
    result = [float(item) for item in vector]
    return result if any(item != 0 for item in result) else None


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise _EmbeddingError("invalid_response")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    if not denominator:
        raise _EmbeddingError("invalid_response")
    score = numerator / denominator
    if not math.isfinite(score):
        raise _EmbeddingError("invalid_response")
    return max(-1.0, min(1.0, score))


class EmbeddingIndex:
    """Compute real model similarities while reusing document vectors per authorization scope."""

    def __init__(self, data_dir, *, db=None, artifacts=None):
        self.store = JsonlStore(Path(data_dir) / "indexes" / "embeddings.jsonl", fail_closed=True)
        self.db = db
        self.artifacts = artifacts
        self._locks = WeakValueDictionary()
        self._transport = None

    def _lock(self, endpoint: str, model: str, scope: str) -> asyncio.Lock:
        identity = hashlib.sha256(
            json.dumps([endpoint, model, scope], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        return self._locks.setdefault(identity, asyncio.Lock())

    @staticmethod
    def _cache_key(endpoint: str, model: str, scope: str, document_id: str, content_hash: str) -> str:
        values = [endpoint, model, scope, document_id, content_hash]
        return hashlib.sha256(
            json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    async def _post(self, endpoint: str, model: str, api_key: str, timeout: float,
                    inputs: list[str]) -> list[list[float]]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    trust_env=False,
                    follow_redirects=False,
                    verify=True,
                    timeout=httpx.Timeout(timeout),
                ) as client,
                client.stream("POST", endpoint, headers=headers, json={
                    "model": model,
                    "input": inputs,
                    "encoding_format": "float",
                }) as response,
            ):
                if response.status_code < 200 or response.status_code >= 300:
                    raise _EmbeddingError("upstream_error")
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > MAX_RESPONSE_BYTES:
                            raise _EmbeddingError("response_too_large")
                    except ValueError:
                        raise _EmbeddingError("invalid_response") from None
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise _EmbeddingError("response_too_large")
                    chunks.append(chunk)
        except (httpx.TimeoutException, TimeoutError):
            raise _EmbeddingError("timeout") from None
        except _EmbeddingError:
            raise
        except httpx.HTTPError:
            raise _EmbeddingError("network_error") from None
        try:
            payload = json.loads(b"".join(chunks))
        except (ValueError, UnicodeDecodeError):
            raise _EmbeddingError("invalid_response") from None
        return _validated_vectors(payload, len(inputs))

    async def _embed(self, endpoint: str, model: str, api_key: str, timeout: float,
                     inputs: list[str], *, authorize=None) -> list[list[float]]:
        results = []
        batch = []
        batch_chars = 0
        try:
            async with asyncio.timeout(timeout):
                for value in inputs:
                    if batch and (len(batch) >= MAX_BATCH_INPUTS or batch_chars + len(value) > MAX_BATCH_CHARS):
                        if authorize:
                            await authorize()
                        results.extend(await self._post(endpoint, model, api_key, timeout, batch))
                        batch, batch_chars = [], 0
                    batch.append(value)
                    batch_chars += len(value)
                if batch:
                    if authorize:
                        await authorize()
                    results.extend(await self._post(endpoint, model, api_key, timeout, batch))
        except TimeoutError:
            raise _EmbeddingError("timeout") from None
        return results

    async def scores(self, scope: str, documents: list[dict], query: str,
                     config: dict, *, authorize=None) -> tuple[dict[str, float], dict]:
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return {}, {"mode": "keyword", "semantic_status": "disabled"}
        try:
            endpoint, model, api_key, timeout, minimum = _settings(config)
            if not isinstance(scope, str) or not isinstance(query, str) or not isinstance(documents, list):
                raise _EmbeddingError("invalid_input")
            if not query.strip() or not documents:
                return {}, {"mode": "hybrid", "semantic_status": "ready"}
            if len(documents) > MAX_DOCUMENTS:
                raise _EmbeddingError("input_too_large")

            prepared = []
            cache_refs = {}
            cache_capabilities = {}
            seen = set()
            truncated = 0
            for item in documents:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                    raise _EmbeddingError("invalid_input")
                if item["id"] in seen or not isinstance(item.get("text"), str):
                    raise _EmbeddingError("invalid_input")
                seen.add(item["id"])
                original_text = item["text"]
                content_hash = hashlib.sha256(original_text.encode()).hexdigest()
                text = original_text
                if len(text) > MAX_TEXT_CHARS:
                    text = text[:MAX_TEXT_CHARS]
                    truncated += 1
                prepared.append((item["id"], text, content_hash))
                capability = item.get("cache_capability")
                if isinstance(capability, list) and len(capability) == 4 and all(isinstance(v, str) for v in capability):
                    cache_capabilities[item["id"]] = capability
                reference = item.get("cache_ref")
                if (isinstance(reference, (list, tuple)) and len(reference) == 2
                        and isinstance(reference[0], str) and type(reference[1]) is int):
                    cache_refs[item["id"]] = list(reference)

            query_text = query[:MAX_TEXT_CHARS]
            metadata = {"mode": "hybrid", "semantic_status": "ready"}
            if truncated:
                metadata["truncated_documents"] = truncated
            if len(query) > MAX_TEXT_CHARS:
                metadata["truncated_query"] = True

            async with asyncio.timeout(timeout):
                async with self._lock(endpoint, model, scope):
                    vectors = {}
                    missing = []
                    for document_id, text, content_hash in prepared:
                        key = self._cache_key(endpoint, model, scope, document_id, content_hash)
                        cached = _valid_cached(self.store.get(key))
                        if cached is None:
                            missing.append((document_id, text, key))
                        else:
                            vectors[document_id] = cached

                    # Empty document id is reserved for query vectors (document ids are nonempty).
                    # Reuse the same query vector so provider numerical jitter cannot invalidate pagination.
                    query_key = self._cache_key(endpoint, model, scope, "", hashlib.sha256(query.encode()).hexdigest())
                    query_vector = _valid_cached(self.store.get(query_key))
                    if query_vector is None:
                        missing.insert(0, ("", query_text, query_key))
                    else:
                        vectors[""] = query_vector
                    embedded = await self._embed(
                        endpoint, model, api_key, timeout,
                        [text for _document_id, text, _key in missing],
                        authorize=authorize,
                    )
                    cache_failed = False
                    query_refs = sorted({tuple(ref) for ref in cache_refs.values()})
                    hashes = {identity: content_hash for identity, _text, content_hash in prepared}
                    for (document_id, _text, key), vector in zip(missing, embedded, strict=True):
                        vectors[document_id] = vector
                        try:
                            await asyncio.to_thread(self.store.set, key, {
                                "vector": vector, "cache_version": 2,
                                "capability": cache_capabilities.get(document_id),
                                "content_hash": hashes.get(document_id),
                                "model_hash": self._model_hash(endpoint, model),
                                "references": [cache_refs[document_id]] if document_id in cache_refs else (
                                    [list(ref) for ref in query_refs] if not document_id else []
                                ),
                            })
                        except OSError:
                            cache_failed = True
                    if cache_failed:
                        metadata["reason"] = "cache_write_failed"

                    query_vector = vectors[""]
                    result = {}
                    for document_id, _text, _content_hash in prepared:
                        score = _cosine(query_vector, vectors[document_id])
                        if score >= minimum:
                            result[document_id] = score
                    return result, metadata
        except (asyncio.CancelledError, GatewayError, HTTPException):
            raise
        except TimeoutError:
            return {}, {"mode": "keyword", "semantic_status": "unavailable", "reason": "timeout"}
        except _EmbeddingError as exc:
            return {}, {"mode": "keyword", "semantic_status": "unavailable", "reason": exc.code}
        except Exception:  # noqa: BLE001 -- semantic search must fail closed to keyword mode.
            return {}, {"mode": "keyword", "semantic_status": "unavailable", "reason": "internal_error"}

    @staticmethod
    def _model_hash(endpoint: str, model: str) -> str:
        return hashlib.sha256(
            json.dumps([endpoint, model], separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def candidate_key(candidate) -> str:
        return candidate.generation_id + "\x1f" + candidate.original_name

    @staticmethod
    def _scope_map(scope):
        if isinstance(scope, CatalogScope):
            return {scope.server_id: scope}
        if isinstance(scope, dict) and all(
            isinstance(key, str) and isinstance(value, CatalogScope)
            for key, value in scope.items()
        ):
            return scope
        raise _EmbeddingError("invalid_input")

    async def _validate_generations(self, scopes, generation_ids):
        async with self.db.session() as session:
            generations = {
                row.id: row
                for row in (
                    await session.scalars(
                        select(CatalogGeneration).where(
                            CatalogGeneration.id.in_(generation_ids)
                        )
                    )
                ).all()
            }
            for generation_id in generation_ids:
                generation = generations.get(generation_id)
                if generation is None or generation.status != "ready":
                    raise GatewayError("catalog_changed", "Catalog changed during semantic retrieval")
                scope = scopes.get(generation.server_id)
                if scope is None or generation_scope(generation) != scope:
                    raise GatewayError("unauthorized", "Semantic catalog scope is no longer authorized")
                head = await session.get(CatalogHead, scope_key(scope))
                if (
                    head is None
                    or head.generation_id != generation_id
                    or head.status != "ready"
                ):
                    raise GatewayError("catalog_changed", "Catalog changed during semantic retrieval")

    async def build_generation_vectors(self, scope, generation: str, config: dict) -> dict:
        """Build immutable document vectors in batches outside interactive queries."""
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return {"status": "disabled", "built": 0}
        if self.db is None or self.artifacts is None or not isinstance(scope, CatalogScope):
            raise _EmbeddingError("invalid_input")
        endpoint, model, api_key, _timeout, _minimum = _settings(config)
        model_hash = self._model_hash(endpoint, model)
        build_timeout = config.get("build_timeout_seconds", 30.0)
        if not _number(build_timeout):
            build_timeout = 30.0
        build_timeout = max(0.1, min(30.0, float(build_timeout)))
        built = 0
        async with self._lock(endpoint, model, "build:" + generation):
            await self._validate_generations({scope.server_id: scope}, {generation})
            while True:
                indexed = exists().where(
                    EmbeddingVector.generation_id == CatalogEntry.generation_id,
                    EmbeddingVector.entry_id == CatalogEntry.id,
                    EmbeddingVector.model_hash == model_hash,
                )
                async with self.db.session() as session:
                    rows = list(
                        (
                            await session.execute(
                                select(
                                    CatalogEntry.id,
                                    CatalogEntry.generation_id,
                                    CatalogEntry.search_text,
                                )
                                .where(
                                    CatalogEntry.generation_id == generation,
                                    CatalogEntry.kind == "tools",
                                    ~indexed,
                                )
                                .order_by(CatalogEntry.sort_key, CatalogEntry.name_hash)
                                .limit(250)
                            )
                        ).all()
                    )
                if not rows:
                    break
                for offset in range(0, len(rows), MAX_BATCH_INPUTS):
                    batch = rows[offset:offset + MAX_BATCH_INPUTS]
                    texts = [row.search_text[:MAX_TEXT_CHARS] for row in batch]
                    vectors = await self._embed(
                        endpoint,
                        model,
                        api_key,
                        build_timeout,
                        texts,
                    )
                    ref = await self.artifacts.put_json(
                        {"vectors": vectors},
                        max_bytes=MAX_RESPONSE_BYTES,
                    )
                    async with self.db.locked() as session:
                        current = await session.get(
                            CatalogGeneration,
                            generation,
                            populate_existing=True,
                        )
                        head = await session.get(CatalogHead, scope_key(scope))
                        if (
                            current is None
                            or current.status != "ready"
                            or generation_scope(current) != scope
                            or head is None
                            or head.generation_id != generation
                            or head.status != "ready"
                        ):
                            raise GatewayError(
                                "catalog_changed",
                                "Catalog changed while publishing semantic vectors",
                            )
                        pending = []
                        for index, (entry, vector) in enumerate(zip(batch, vectors, strict=True)):
                            identity = (generation, entry.id, model_hash)
                            if await session.get(EmbeddingVector, identity) is None:
                                pending.append((index, entry, vector))
                        if not pending:
                            continue
                        if await session.get(ArtifactObject, ref.id) is None:
                            session.add(
                                ArtifactObject(
                                    id=ref.id,
                                    size_bytes=ref.size_bytes,
                                    sha256=ref.sha256,
                                )
                            )
                            await session.flush()
                        for index, entry, vector in pending:
                            session.add(
                                EmbeddingVector(
                                    generation_id=generation,
                                    entry_id=entry.id,
                                    model_hash=model_hash,
                                    scope_key=scope_key(scope),
                                    owner=scope.owner,
                                    content_hash=hashlib.sha256(
                                        entry.search_text.encode()
                                    ).hexdigest(),
                                    artifact_id=ref.id,
                                    artifact_bytes=ref.size_bytes,
                                    vector_index=index,
                                    dimension=len(vector),
                                )
                            )
                        built += len(pending)
                if len(rows) < 250:
                    break
        return {"status": "ready", "built": built, "model_hash": model_hash}

    async def build_pending(self, config: dict, *, generation_limit: int = 1) -> dict:
        """Incrementally build vectors for current ready generations."""
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return {"status": "disabled", "generations": 0, "built": 0}
        if self.db is None or self.artifacts is None:
            return {"status": "unavailable", "generations": 0, "built": 0}
        endpoint, model, _api_key, _timeout, _minimum = _settings(config)
        model_hash = self._model_hash(endpoint, model)
        indexed_entry = exists().where(
            EmbeddingVector.generation_id == CatalogEntry.generation_id,
            EmbeddingVector.entry_id == CatalogEntry.id,
            EmbeddingVector.model_hash == model_hash,
        )
        missing_vector = exists().where(
            CatalogEntry.generation_id == CatalogGeneration.id,
            CatalogEntry.kind == "tools",
            ~indexed_entry,
        )
        async with self.db.session() as session:
            generations = list(
                (
                    await session.scalars(
                        select(CatalogGeneration)
                        .join(
                            CatalogHead,
                            CatalogHead.generation_id == CatalogGeneration.id,
                        )
                        .where(
                            CatalogGeneration.status == "ready",
                            CatalogHead.status == "ready",
                            missing_vector,
                        )
                        .order_by(CatalogGeneration.published_at, CatalogGeneration.id)
                        .limit(max(1, min(16, generation_limit)))
                    )
                ).all()
            )
        total = 0
        for generation in generations:
            result = await self.build_generation_vectors(
                generation_scope(generation),
                generation.id,
                config,
            )
            total += result["built"]
        return {"status": "ready", "generations": len(generations), "built": total}

    async def score_candidates(
        self,
        scope,
        refs,
        query: str,
        config: dict,
        *,
        deadline: float = 2.0,
        authorize=None,
    ) -> tuple[dict[str, float], dict]:
        """Embed only the query and score at most 200 preselected catalog references."""
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return {}, {"mode": "keyword", "semantic_status": "disabled"}
        try:
            if self.db is None or self.artifacts is None:
                raise _EmbeddingError("cold_index")
            candidates = list(refs)[:200]
            if not candidates or not isinstance(query, str) or not query.strip():
                return {}, {"mode": "hybrid", "semantic_status": "ready"}
            endpoint, model, api_key, timeout, minimum = _settings(config)
            model_hash = self._model_hash(endpoint, model)
            scopes = self._scope_map(scope)
            generation_ids = {row.generation_id for row in candidates}
            await self._validate_generations(scopes, generation_ids)
            if authorize:
                await authorize()
            entry_ids = [row.id for row in candidates]
            async with asyncio.timeout(max(0.001, min(float(deadline), timeout))):
                async with self.db.session() as session:
                    metadata = list(
                        (
                            await session.scalars(
                                select(EmbeddingVector).where(
                                    EmbeddingVector.entry_id.in_(entry_ids),
                                    EmbeddingVector.model_hash == model_hash,
                                )
                            )
                        ).all()
                    )
                by_entry = {row.entry_id: row for row in metadata}
                if len(by_entry) != len(candidates):
                    raise _EmbeddingError("cold_index")
                for candidate in candidates:
                    vector = by_entry[candidate.id]
                    expected_scope = scopes.get(candidate.server_id)
                    if (
                        expected_scope is None
                        or vector.generation_id != candidate.generation_id
                        or vector.scope_key != scope_key(expected_scope)
                        or vector.owner != expected_scope.owner
                        or vector.content_hash
                        != hashlib.sha256(candidate.search_text.encode()).hexdigest()
                    ):
                        raise _EmbeddingError("cold_index")
                artifacts = {}
                for vector in metadata:
                    if vector.artifact_id not in artifacts:
                        artifacts[vector.artifact_id] = await self.artifacts.read_json(
                            ArtifactRef(
                                vector.artifact_id,
                                vector.artifact_bytes,
                                vector.artifact_id,
                            ),
                            max_bytes=MAX_RESPONSE_BYTES,
                        )
                query_vector = (
                    await self._embed(
                        endpoint,
                        model,
                        api_key,
                        min(timeout, float(deadline)),
                        [query[:MAX_TEXT_CHARS]],
                        authorize=authorize,
                    )
                )[0]
                if authorize:
                    await authorize()
                await self._validate_generations(scopes, generation_ids)
                scores = {}
                for candidate in candidates:
                    metadata_row = by_entry[candidate.id]
                    payload = artifacts[metadata_row.artifact_id]
                    vectors = payload.get("vectors") if isinstance(payload, dict) else None
                    if (
                        not isinstance(vectors, list)
                        or not 0 <= metadata_row.vector_index < len(vectors)
                    ):
                        raise _EmbeddingError("invalid_response")
                    score = _cosine(query_vector, vectors[metadata_row.vector_index])
                    if score >= minimum:
                        scores[self.candidate_key(candidate)] = score
                return scores, {"mode": "hybrid", "semantic_status": "ready"}
        except (asyncio.CancelledError, GatewayError, HTTPException):
            raise
        except TimeoutError:
            return {}, {
                "mode": "keyword",
                "semantic_status": "unavailable",
                "reason": "timeout",
            }
        except _EmbeddingError as exc:
            return {}, {
                "mode": "keyword",
                "semantic_status": "unavailable",
                "reason": exc.code,
            }
        except Exception:  # noqa: BLE001 -- semantic failure preserves keyword results.
            return {}, {
                "mode": "keyword",
                "semantic_status": "unavailable",
                "reason": "internal_error",
            }

    async def probe(self, config: dict) -> dict:
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return {"status": "disabled", "semantic_status": "disabled"}
        try:
            endpoint, model, api_key, timeout, _minimum = _settings(config)
            vector = (await self._embed(endpoint, model, api_key, timeout, [PROBE_TEXT]))[0]
            return {
                "status": "ready",
                "semantic_status": "ready",
                "dimension": len(vector),
                "model": model,
                "endpoint": endpoint,
            }
        except asyncio.CancelledError:
            raise
        except _EmbeddingError as exc:
            return {"status": "unavailable", "semantic_status": "unavailable", "reason": exc.code}
        except Exception:  # noqa: BLE001 -- diagnostics return only safe error codes.
            return {"status": "unavailable", "semantic_status": "unavailable", "reason": "internal_error"}


async def prune_stale_vectors(state, *, batch_limit=250, server_id=None, startup=False):
    """Reclaim rebuildable vectors without changing catalog or disabled/failed scopes."""
    from .embedding_api import load_embedding_config

    if type(batch_limit) is not int or not 0 <= batch_limit <= 250:
        raise ValueError("Vector retention batch must contain 0–250 items")
    config = await load_embedding_config(state)
    model_hash = None
    if config.get("enabled") is True:
        try:
            endpoint, model, *_ = _settings(config)
            model_hash = EmbeddingIndex._model_hash(endpoint, model)
        except _EmbeddingError:
            pass  # An invalid setting is not authority to remove a previous model.
    disabled = exists().where(
        CatalogGeneration.id == EmbeddingVector.generation_id,
        McpServer.id == CatalogGeneration.server_id, McpServer.mode == "disabled",
    )
    failed = exists().where(
        CatalogHead.scope_key == EmbeddingVector.scope_key, CatalogHead.status != "ready",
    )
    referenced = exists().where(
        CatalogEntry.id == EmbeddingVector.entry_id,
        CatalogEntry.generation_id == EmbeddingVector.generation_id,
        or_(CatalogHead.generation_id == CatalogEntry.generation_id,
            CatalogHead.refresh_id == CatalogEntry.generation_id),
    )
    stale = and_(~disabled, ~failed, or_(~referenced, EmbeddingVector.model_hash != model_hash)
                 if model_hash else ~referenced)
    if server_id is not None:
        stale = and_(stale, exists().where(
            CatalogGeneration.id == EmbeddingVector.generation_id,
            CatalogGeneration.server_id == server_id,
        ))
    deleted = 0
    if batch_limit:
        async with state.db.locked() as session:
            identities = list((await session.execute(select(
                EmbeddingVector.generation_id, EmbeddingVector.entry_id, EmbeddingVector.model_hash,
            ).where(stale).limit(batch_limit))).all())
            if identities:
                result = await session.execute(delete(EmbeddingVector).where(
                    stale, tuple_(EmbeddingVector.generation_id, EmbeddingVector.entry_id,
                                  EmbeddingVector.model_hash).in_(identities),
                ).execution_options(synchronize_session=False))
                deleted = result.rowcount

    # Only small reference metadata is loaded; vector payloads stay out of SQL and the event loop.
    async with state.db.session() as session:
        services = {row.id: (row.revision, row.mode) for row in (await session.execute(
            select(McpServer.id, McpServer.revision, McpServer.mode)
        )).all()}
        protected = set((await session.scalars(select(CatalogHead.server_id).where(
            CatalogHead.status != "ready"
        ))).all())

    def capability_providers():
        with state.embeddings.store.lock:
            return list({value["capability"][0] for value in state.embeddings.store.items.values()
                         if isinstance(value, dict) and isinstance(value.get("capability"), list)
                         and len(value["capability"]) == 4
                         and (server_id is None or value["capability"][0] == server_id)})

    provider_ids = await asyncio.to_thread(capability_providers)
    live_capabilities = {}
    indexed_owners = set()
    for offset in range(0, len(provider_ids), 250):
        async with state.db.session() as session:
            indexed_owners.update((await session.execute(select(CatalogHead.server_id, CatalogHead.owner).where(
                CatalogHead.server_id.in_(provider_ids[offset:offset + 250]),
                CatalogHead.generation_id.is_not(None),
            ))).all())
            records = (await session.execute(select(
                CatalogGeneration.server_id, CatalogGeneration.owner,
                CatalogEntry.kind, CatalogEntry.name_hash, CatalogEntry.summary,
            ).join(CatalogEntry, CatalogEntry.generation_id == CatalogGeneration.id).join(
                CatalogHead, CatalogHead.generation_id == CatalogGeneration.id,
            ).where(CatalogGeneration.server_id.in_(provider_ids[offset:offset + 250]),
                    CatalogEntry.kind != "tools"))).all()
            live_capabilities.update({(provider, owner, kind, name_hash): summary.get("semantic_hash")
                                      for provider, owner, kind, name_hash, summary in records})

    def clean_cache():
        store = state.embeddings.store
        with store.lock:
            doomed = set()
            for key, value in store.items.items():
                if not isinstance(value, dict) or value.get("cache_version") != 2:
                    if startup:
                        doomed.add(key)  # One-time rebuild of untraceable legacy hashes.
                    continue
                refs = value.get("references")
                if not isinstance(refs, list) or any(
                    not isinstance(ref, list) or len(ref) != 2 or not isinstance(ref[0], str)
                    or type(ref[1]) is not int for ref in refs
                ):
                    if startup:
                        doomed.add(key)
                    continue
                if server_id is not None and not any(ref[0] == server_id for ref in refs):
                    continue
                if any(ref[0] not in services for ref in refs):
                    doomed.add(key)
                    continue
                if any(ref[0] in protected or services.get(ref[0], (None, None))[1] == "disabled" for ref in refs):
                    continue
                if model_hash and value.get("model_hash") != model_hash:
                    doomed.add(key)
                    continue
                capability = value.get("capability")
                if isinstance(capability, list) and len(capability) == 4:
                    identity = tuple(capability)
                    current_hash = live_capabilities.get(identity)
                    provider, owner, kind, name_hash = capability
                    if current_hash is None and (provider, owner) not in indexed_owners:
                        # A pre-index catalog is still authoritative for the legacy resource route.
                        cache_key = provider + "-" + hashlib.sha256(owner.encode()).hexdigest()[:24]
                        with state.catalog.cache_store.lock:
                            record = state.catalog.cache_store.items.get(cache_key, {})
                            data = record.get("data", {}) if isinstance(record, dict) else {}
                            for item in data.get(kind, []) if isinstance(data, dict) else []:
                                name = item.get("uri") if kind == "resources" else item.get("uriTemplate") if kind == "templates" else item.get("name")
                                if isinstance(name, str) and hashlib.sha256(name.encode()).hexdigest() == name_hash:
                                    current_hash = resource_vector_hash(item)
                                    break
                    if current_hash != value.get("content_hash"):
                        doomed.add(key)
                elif any(services.get(identity, (None, None))[0] != revision for identity, revision in refs):
                    doomed.add(key)
            if doomed:
                return store.delete_keys(doomed)
            if startup:
                store.compact()
            return 0

    cached = await asyncio.to_thread(clean_cache)
    return {"vectors": deleted, "cache_entries": cached}


async def reuse_generation_vectors(session, previous, generation):
    """Rebind unchanged same-owner tools without reading payloads or calling a model."""
    if not previous:
        return
    old, new = aliased(CatalogEntry), aliased(CatalogEntry)
    offset = 0
    while True:
        rows = list((await session.execute(select(
            EmbeddingVector, new.id.label("new_entry_id"), new.search_text,
        ).join(old, old.id == EmbeddingVector.entry_id).join(new, and_(
            new.generation_id == generation.id, new.kind == "tools",
            new.name_hash == old.name_hash, new.schema_hash == old.schema_hash,
        )).where(
            EmbeddingVector.generation_id == previous,
            EmbeddingVector.scope_key == generation.scope_key,
            EmbeddingVector.owner == generation.owner,
        ).order_by(new.id, EmbeddingVector.model_hash).offset(offset).limit(250))).all())
        if not rows:
            return

        def prepare(candidates=rows):
            values = []
            for vector, entry_id, text in candidates:
                if vector.content_hash != hashlib.sha256(text.encode()).hexdigest():
                    continue
                values.append({
                    "generation_id": generation.id, "entry_id": entry_id,
                    "model_hash": vector.model_hash, "scope_key": generation.scope_key,
                    "owner": generation.owner, "content_hash": vector.content_hash,
                    "artifact_id": vector.artifact_id, "artifact_bytes": vector.artifact_bytes,
                    "vector_index": vector.vector_index, "dimension": vector.dimension,
                })
            return values

        values = await asyncio.to_thread(prepare)
        if values:
            await session.execute(insert(EmbeddingVector), values)
        offset += len(rows)


def resource_vector_hash(entry):
    """Match legacy descriptive embedding input exactly; timestamps and revisions are excluded."""
    text = {key: entry[key] for key in ("name", "title", "description") if isinstance(entry.get(key), str)}
    return hashlib.sha256(json.dumps(text, ensure_ascii=False).encode()).hexdigest()
