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

    def __init__(self, data_dir):
        self.store = JsonlStore(Path(data_dir) / "indexes" / "embeddings.jsonl", fail_closed=True)
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
                    for (document_id, _text, key), vector in zip(missing, embedded, strict=True):
                        vectors[document_id] = vector
                        try:
                            await asyncio.to_thread(self.store.set, key, {"vector": vector})
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
