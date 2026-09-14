import json
import math

import httpx
import pytest

from mcp_manager.embeddings import EmbeddingIndex
from mcp_manager.tool_index import document, rank


def config(**overrides):
    value = {
        "enabled": True,
        "base_url": "http://embedding.test/v1",
        "model": "fixture-model",
        "api_key": "fixture-secret",
        "timeout_seconds": 2,
        "min_similarity": -1,
    }
    value.update(overrides)
    return value


def install_transport(index, handler):
    index._transport = httpx.MockTransport(handler)


async def test_protocol_reorders_indexes_and_scores_real_vectors(tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        assert request.url == httpx.URL("http://embedding.test/v1/embeddings")
        assert request.headers["authorization"] == "Bearer fixture-secret"
        assert payload == {
            "model": "fixture-model",
            "input": ["find meteorology", "barometer instruments", "bakery ovens"],
            "encoding_format": "float",
        }
        return httpx.Response(200, json={"data": [
            {"index": 2, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 1, "embedding": [0.9, 0.1]},
        ]})

    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)
    scores, metadata = await index.scores(
        "token:user-1",
        [{"id": "weather", "text": "barometer instruments"}, {"id": "food", "text": "bakery ovens"}],
        "find meteorology",
        config(),
    )

    assert scores["weather"] > 0.99
    assert scores["food"] == 0.0
    assert metadata == {"mode": "hybrid", "semantic_status": "ready"}
    assert len(requests) == 1


@pytest.mark.parametrize("response", [
    {},
    {"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
    {"data": [
        {"index": 0, "embedding": [1.0, 0.0]},
        {"index": 0, "embedding": [0.0, 1.0]},
    ]},
    {"data": [
        {"index": 0, "embedding": [1.0, 0.0]},
        {"index": True, "embedding": [0.0, 1.0]},
    ]},
    {"data": [
        {"index": 0, "embedding": [1.0, 0.0]},
        {"index": 1, "embedding": [1.0]},
    ]},
    {"data": [
        {"index": 0, "embedding": [1.0, 0.0]},
        {"index": 1, "embedding": [0.0, 0.0]},
    ]},
    {"data": [
        {"index": 0, "embedding": [1.0, 0.0]},
        {"index": 1, "embedding": [math.nan, 1.0]},
    ]},
])
async def test_invalid_embedding_responses_degrade_without_scores(tmp_path, response):
    index = EmbeddingIndex(tmp_path)
    install_transport(index, lambda request: httpx.Response(200, content=json.dumps(response).encode()))

    scores, metadata = await index.scores(
        "scope", [{"id": "doc", "text": "document"}], "query", config()
    )

    assert scores == {}
    assert metadata == {
        "mode": "keyword",
        "semantic_status": "unavailable",
        "reason": "invalid_response",
    }


async def test_timeout_degrades_to_keyword(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("private upstream detail", request=request)

    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)
    scores, metadata = await index.scores(
        "scope", [{"id": "doc", "text": "document"}], "query", config()
    )

    assert scores == {}
    assert metadata == {
        "mode": "keyword",
        "semantic_status": "unavailable",
        "reason": "timeout",
    }


async def test_total_timeout_degrades_to_keyword(tmp_path):
    async def handler(request):
        import asyncio
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"data": [
            {"index": 0, "embedding": [1.0]},
            {"index": 1, "embedding": [1.0]},
        ]})

    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)
    scores, metadata = await index.scores(
        "scope", [{"id": "doc", "text": "document"}], "query",
        config(timeout_seconds=0.01),
    )

    assert scores == {}
    assert metadata["reason"] == "timeout"


async def test_total_timeout_includes_waiting_for_same_scope_lock(tmp_path):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            entered.set()
            await release.wait()
        payload = json.loads(request.content)
        return httpx.Response(200, json={"data": [
            {"index": position, "embedding": [1.0, float(position + 1)]}
            for position in range(len(payload["input"]))
        ]})

    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)
    first = asyncio.create_task(index.scores(
        "same-scope", [{"id": "doc", "text": "document"}], "first", config(timeout_seconds=2)
    ))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(index.scores(
        "same-scope", [{"id": "doc", "text": "document"}], "second", config(timeout_seconds=0.01)
    ))
    await asyncio.sleep(0.03)
    release.set()

    _first_result, second_result = await asyncio.gather(first, second)
    assert second_result == (
        {},
        {"mode": "keyword", "semantic_status": "unavailable", "reason": "timeout"},
    )


async def test_incremental_cache_is_persistent_and_isolated_by_scope_model_endpoint_and_content(tmp_path):
    batches = []

    def handler(request):
        payload = json.loads(request.content)
        batches.append((str(request.url), payload["model"], list(payload["input"])))
        return httpx.Response(200, json={"data": [
            {"index": position, "embedding": [1.0, float(position + 1)]}
            for position in range(len(payload["input"]))
        ]})

    documents = [{"id": "tool-a", "text": "first content"}]
    first = EmbeddingIndex(tmp_path)
    install_transport(first, handler)
    await first.scores("scope-a", documents, "query", config())
    await first.scores("scope-a", documents, "query", config())
    second = EmbeddingIndex(tmp_path)
    install_transport(second, handler)
    await second.scores("scope-a", documents, "query", config())
    await second.scores("scope-b", documents, "query", config())
    await second.scores("scope-a", [{"id": "tool-a", "text": "changed content"}], "query", config())
    await second.scores("scope-a", documents, "query", config(model="other-model"))
    await second.scores(
        "scope-a", documents, "query",
        config(base_url="http://second.test/v1/embeddings"),
    )

    assert [len(batch[2]) for batch in batches] == [2, 2, 1, 2, 2]
    assert batches[-1][0] == "http://second.test/v1/embeddings"
    persisted = (tmp_path / "indexes" / "embeddings.jsonl").read_text(encoding="utf-8")
    assert "first content" not in persisted
    assert "query" not in persisted
    assert "fixture-secret" not in persisted
    assert "[1.0, 2.0]" in persisted


async def test_disabled_and_empty_inputs_never_call_http(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("network must not be called")

    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)

    assert await index.scores("scope", [{"id": "d", "text": "x"}], "q", config(enabled=False)) == (
        {}, {"mode": "keyword", "semantic_status": "disabled"}
    )
    assert await index.scores("scope", [], "q", config()) == (
        {}, {"mode": "hybrid", "semantic_status": "ready"}
    )
    assert await index.scores("scope", [{"id": "d", "text": "x"}], "", config()) == (
        {}, {"mode": "hybrid", "semantic_status": "ready"}
    )
    assert calls == []


async def test_text_is_bounded_and_similarity_threshold_is_applied(tmp_path):
    inputs = []

    def handler(request):
        payload = json.loads(request.content)
        inputs.extend(payload["input"])
        return httpx.Response(200, json={"data": [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 1, "embedding": [0.5, math.sqrt(0.75)]},
        ]})

    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)
    scores, metadata = await index.scores(
        "scope", [{"id": "doc", "text": "x" * 50_000}], "query",
        config(min_similarity=0.6),
    )

    assert scores == {}
    assert len(inputs[1]) == 16_000
    assert metadata == {
        "mode": "hybrid",
        "semantic_status": "ready",
        "truncated_documents": 1,
    }


async def test_bare_origin_uses_openai_v1_embeddings_path_without_retry(tmp_path):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)
    result = await index.probe(config(base_url="http://192.168.2.168:8098"))

    assert result["status"] == "ready"
    assert urls == ["http://192.168.2.168:8098/v1/embeddings"]


async def test_probe_uses_harmless_sample_and_returns_safe_diagnostics(tmp_path):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]})

    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)

    result = await index.probe(config(base_url="https://name:password@embedding.test/v1?secret=value"))

    assert seen["input"] == ["MCP semantic discovery probe"]
    assert result == {
        "status": "ready",
        "semantic_status": "ready",
        "dimension": 3,
        "model": "fixture-model",
        "endpoint": "https://embedding.test/v1/embeddings",
    }
    assert "password" not in str(result)
    assert "secret" not in str(result)


async def test_query_vector_is_reused_across_pages_and_restart_despite_model_jitter(tmp_path):
    calls = []

    def handler(request):
        inputs = json.loads(request.content)["input"]
        calls.append(inputs)
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [1.0, (0.1 if len(calls) == 1 else 0.9) + i]}
            for i in range(len(inputs))]})

    documents = [{"id": "first", "text": "document a"}, {"id": "second", "text": "document b"}]
    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)
    first = await index.scores("owner", documents, "same query", config())
    restarted = EmbeddingIndex(tmp_path)
    install_transport(restarted, handler)
    second = await restarted.scores("owner", documents, "same query", config())
    assert first == second
    assert len(calls) == 1


async def test_missing_similarity_setting_defaults_to_point_five_in_embedding_client(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 1, "embedding": [0.4, math.sqrt(0.84)]},
        ]})

    settings = config()
    settings.pop("min_similarity")
    index = EmbeddingIndex(tmp_path)
    install_transport(index, handler)
    scores, metadata = await index.scores(
        "scope", [{"id": "doc", "text": "document"}], "query", settings,
    )
    assert metadata["semantic_status"] == "ready"
    assert scores == {}


async def test_missing_similarity_setting_defaults_to_point_five_in_rank():
    class Embeddings:
        async def scores(self, scope, documents, query, config, *, authorize=None):
            return {"doc": 0.4}, {"mode": "hybrid", "semantic_status": "ready"}

    ranked, _status = await rank(
        [document("doc", ["other"], [("unrelated text", 1)])],
        "query", Embeddings(), {"enabled": True}, "scope",
    )
    assert ranked == []
