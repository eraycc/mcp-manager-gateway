"""Regression coverage for embedded resources and cold HTTP parameter headers."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from starlette.requests import Request
from test_catalog_paged_api import indexed_catalog
from test_exact_route_scalability import gateway_context, invoke

from mcp_manager.catalog import alias
from mcp_manager.database import ApiToken


async def enabled_context(harness, *, inject=True):
    ctx, token_id = await gateway_context(harness)
    if inject:
        response = await harness.web.patch("/api/v1/settings", json={"embedded_resource.inject": True})
        assert response.status_code == 200
    async with harness.db.locked() as session:
        (await session.get(ApiToken, token_id)).enable_resource_tools = True
    gateway = harness.app.state.gateway
    user, token, _ = await gateway.principal(ctx.request)
    lease = harness.app.state.runtime.create_lease(user.id, token.id, kind="retention")
    sid = "fixture-session-" + lease.id
    gateway.sessions[sid] = {"owner": (user.id, token.id), "lease": lease.id, "touched": 0}
    ctx.request = Request({**ctx.request.scope, "headers": [
        *ctx.request.scope["headers"], (b"mcp-session-id", sid.encode()),
    ]})
    return ctx, sid


async def resource_catalog(harness):
    row, tools = await indexed_catalog(harness, 1)
    catalog = harness.app.state.catalog
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    await catalog.index.stage_page(generation.id, "tools", tools)
    await catalog.index.stage_page(generation.id, "resources", [
        {"uri": "fixture://health", "name": "health", "description": "battery status",
         "inputSchema": {"description": "fixture://must-not-be-embedded"}, "extra": "private-extension"},
        {"uri": "fixture://weather", "name": "weather", "description": "weather forecast"},
    ])
    await catalog.index.stage_page(generation.id, "prompts", [{"name": "health", "description": "health prompt"}])
    await catalog.index.stage_page(generation.id, "templates", [
        {"uriTemplate": "fixture://sensor/{key}", "name": "sensor", "description": "sensor lookup"},
    ])
    await catalog.index.publish(generation.id, expected_scope=scope)
    return row, tools


async def test_embedded_roundtrip_session_isolation_and_release(db_app, monkeypatch):
    row, tools = await resource_catalog(db_app)
    ctx, sid = await enabled_context(db_app)
    original = {"uri": "github://file/example/repo/private.txt", "mimeType": "text/plain", "text": "private body"}
    class Connection:
        async def call(self, name, arguments):
            return {"content": [{"type": "text", "text": "downloaded"},
                                {"type": "resource", "resource": original}], "structuredContent": {"ok": True}}
        async def read_resource(self, uri):
            raise AssertionError("Embedded content must be served locally")
    @asynccontextmanager
    async def connector(spec):
        yield Connection()
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    result = await invoke(db_app, ctx, "gateway_call", {"name": alias(row.slug, tools[0]["name"]), "arguments": {}})
    assert not result.is_error
    assert result.content[1].resource.text == "private body"
    assert result.structured_content == {"ok": True}
    page = await invoke(db_app, ctx, "gateway_search_resources", {"kind": "resource", "uri_prefix": "github://"})
    assert not page.is_error, page
    items = page.structured_content["items"]
    assert len(items) == 1 and items[0]["temporary"]
    uri = items[0]["uri"]
    assert uri != original["uri"]
    assert any(uri in getattr(block, "text", "") for block in result.content)
    value = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
    assert value.structured_content["contents"][0]["text"] == "private body"
    native = await db_app.app.state.gateway.list_resources(ctx, SimpleNamespace(cursor=None))
    assert uri in [str(item.uri) for item in native.resources]
    other, _ = await enabled_context(db_app)
    denied = await invoke(db_app, other, "gateway_read_resource", {"uri": uri})
    assert denied.is_error
    await db_app.app.state.gateway.disconnect_lease(db_app.app.state.gateway.sessions[sid]["lease"])
    assert not db_app.app.state.gateway.embedded.items
    denied = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
    assert denied.is_error


async def test_resource_filters_and_keyword_only_never_embed(db_app, monkeypatch):
    await resource_catalog(db_app)
    ctx, _sid = await enabled_context(db_app)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Explicit keyword/exact search must not embed")
    monkeypatch.setattr(db_app.app.state.embeddings, "score_candidates", forbidden)
    for arguments, names in [
        ({"kind": "prompt"}, ["health"]),
        ({"kind": "resource", "keyword": "health", "match": "exact"}, ["health"]),
        ({"kind": "resource", "keyword": "forecast", "match": "keyword_only"}, ["weather"]),
        ({"kind": "template", "uri_prefix": "fixture://sensor"}, ["sensor"]),
    ]:
        page = await invoke(db_app, ctx, "gateway_search_resources", arguments)
        assert not page.is_error, page
        assert [item["name"] for item in page.structured_content["items"]] == names
        assert page.structured_content["search"]["semantic_status"] != "pending_index"


async def test_resource_vectors_include_descriptions_without_uris(db_app, monkeypatch):
    row, _tools = await resource_catalog(db_app)
    state = db_app.app.state
    async def no_background_build(*args, **kwargs):
        return {"built": 0}
    # Keep the manual build's exact row-count assertions independent of the
    # periodic builder, which otherwise may have already built these vectors.
    monkeypatch.setattr(state.embeddings, "build_pending", no_background_build)
    configured = await db_app.web.patch("/api/v1/settings/embedding", json={
        "enabled": True, "base_url": "https://embedding.invalid/v1", "model": "test-model",
        "min_similarity": 0.5,
    })
    assert configured.status_code == 200
    from mcp_manager.embedding_api import load_embedding_config
    config = await load_embedding_config(state)
    inputs = []
    async def embed(endpoint, model, key, timeout, texts, **kwargs):
        inputs.extend(texts)
        return [[1.0, 0.0] for text in texts]
    monkeypatch.setattr(state.embeddings, "_embed", embed)
    scope = await state.catalog.index_scope(row)
    generation = (await state.catalog.index.get_summary(scope))["generation"]
    built = await state.embeddings.build_generation_vectors(scope, generation, config)
    assert built["built"] == 5
    assert any("battery" in text for text in inputs)
    assert all("fixture://" not in text for text in inputs)
    inputs.clear()
    ctx, _sid = await enabled_context(db_app)
    result = await invoke(db_app, ctx, "gateway_search_resources", {"kind": "resource", "keyword": "power level"})
    assert not result.is_error
    assert result.structured_content["count"] == 2
    assert result.structured_content["search"]["semantic_status"] == "ready"
    assert inputs == ["power level"]


def test_embedded_cache_binary_expiry_and_capacity():
    from mcp import types

    from mcp_manager.catalog_types import CatalogScope
    from mcp_manager.embedded_resources import EmbeddedResources

    cache = EmbeddedResources()
    cache.max_session_items = 2
    cache.max_total_items = 3
    scope = CatalogScope("server", "service", 1, "credential-a")
    namespace = ("session", "one")
    result = types.CallToolResult(content=[types.EmbeddedResource(type="resource",
        resource=types.BlobResourceContents(uri="fixture://binary", mimeType="application/octet-stream", blob="AAEC"))])
    uris = [cache.capture(namespace, scope, result)[0][0] for _ in range(3)]
    assert uris[0] not in cache.items
    assert len(cache.items) == 2
    assert cache.read(namespace, scope, uris[1])["contents"][0]["blob"] == "AAEC"
    assert cache.read(("session", "two"), scope, uris[1]) is None
    assert cache.read(namespace, CatalogScope("server", "service", 1, "credential-b"), uris[1]) is None
    for index in range(2):
        cache.capture(("session", str(index)), scope, result)
    assert len(cache.items) == 3
    cache.max_item_bytes = 1
    assert cache.capture(namespace, scope, result) == ([], 1)
    for item in cache.items.values():
        item["expires"] = 0
    cache.prune()
    assert not cache.items and cache.bytes == 0


async def test_fuzzy_resource_typo_and_query_bound_cursor(db_app):
    await resource_catalog(db_app)
    ctx, _ = await enabled_context(db_app)
    result = await invoke(db_app, ctx, "gateway_search_resources", {
        "keyword": "wether", "kind": "resource", "limit": 1,
    })
    assert not result.is_error
    assert [item["name"] for item in result.structured_content["items"]] == ["weather"]
    result = await invoke(db_app, ctx, "gateway_search_resources", {"limit": 1})
    cursor = result.structured_content["next_cursor"]
    assert cursor
    denied = await invoke(db_app, ctx, "gateway_search_resources", {"limit": 1, "kind": "prompt", "cursor": cursor})
    assert denied.is_error
    result = await invoke(db_app, ctx, "gateway_search_resources", {
        "keyword": "HEALTH", "kind": "resource", "match": "exact",
    })
    assert not result.is_error and result.structured_content["count"] == 0


async def test_temporary_resource_rejects_same_token_other_session_and_changed_credentials(db_app):
    from mcp import types

    from mcp_manager.database import SystemSetting

    row, _ = await resource_catalog(db_app)
    ctx, _ = await enabled_context(db_app)
    gateway = db_app.app.state.gateway
    user, token, _ = await gateway.principal(ctx.request)
    value = await gateway.capture_embedded(ctx.request, user, token, row, {
        "content": [{"type": "resource", "resource": {"uri": "fixture://secret", "text": "body"}}],
    })
    assert isinstance(value.content[0], types.EmbeddedResource)
    uri = next(iter(gateway.embedded.items))
    lease = db_app.app.state.runtime.create_lease(user.id, token.id, kind="retention")
    sid = "second-session"
    gateway.sessions[sid] = {"owner": (user.id, token.id), "lease": lease.id, "touched": 0}
    headers = [(key, value) for key, value in ctx.request.scope["headers"] if key != b"mcp-session-id"]
    other = SimpleNamespace(request=Request({**ctx.request.scope, "headers": [*headers, (b"mcp-session-id", sid.encode())]}))
    assert (await invoke(db_app, other, "gateway_read_resource", {"uri": uri})).is_error
    async with db_app.db.locked() as session:
        key = db_app.app.state.catalog.credentials.key(row.id, "service")
        session.add(SystemSetting(key=key, value=db_app.app.state.catalog.seal({"synthetic": "new-credential"})))
    result = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
    assert result.is_error
    page = await invoke(db_app, ctx, "gateway_search_resources", {"uri_prefix": "fixture://secret"})
    assert not page.is_error and not page.structured_content["items"]


async def test_injection_switch_does_not_cache_while_disabled(db_app):
    row, _ = await resource_catalog(db_app)
    ctx, _ = await enabled_context(db_app)
    gateway = db_app.app.state.gateway
    user, token, _ = await gateway.principal(ctx.request)
    key = "embedded_resource.inject"
    invalid = await db_app.web.patch("/api/v1/settings", json={key: "false"})
    assert invalid.status_code == 422
    assert (await db_app.web.patch("/api/v1/settings", json={key: False})).status_code == 200
    original = {"content": [{"type": "text", "text": "downloaded"}, {
        "type": "resource", "resource": {"uri": "fixture://switch", "text": "body"},
    }], "structuredContent": {"original": True}}
    value = await gateway.capture_embedded(ctx.request, user, token, row, original)
    assert value.model_dump(mode="json", by_alias=True, exclude_none=True)["content"] == original["content"]
    assert value.structured_content == original["structuredContent"]
    assert not gateway.embedded.items
    assert (await db_app.web.patch("/api/v1/settings", json={key: True})).status_code == 200
    listed = await invoke(db_app, ctx, "gateway_search_resources", {"uri_prefix": "fixture://switch"})
    assert not listed.is_error and listed.structured_content["items"] == []

async def test_revoking_stateless_lease_clears_embedded_cache(db_app):
    await db_app.web.patch("/api/v1/settings", json={"embedded_resource.inject": True})
    row, _ = await resource_catalog(db_app)
    ctx, token_id = await gateway_context(db_app)
    state = db_app.app.state
    user, token, _ = await state.gateway.principal(ctx.request)
    lease = state.runtime.create_lease(user.id, token.id, kind="bridge")
    ctx.request = Request({**ctx.request.scope, "headers": [
        *ctx.request.scope["headers"], (b"x-mcp-manager-lease", lease.id.encode()),
    ]})
    await state.gateway.capture_embedded(ctx.request, user, token, row, {
        "content": [{"type": "resource", "resource": {"uri": "fixture://private", "text": "body"}}],
    })
    assert state.gateway.embedded.items
    await state.runtime.revoke(token_id=token_id)
    await state.gateway.revoke(token_id=token_id)
    assert not state.gateway.embedded.items


async def test_star_resource_query_browses_all_kinds_with_bounded_pages(db_app, monkeypatch):
    row, _tools = await resource_catalog(db_app)
    ctx, _ = await enabled_context(db_app)
    state = db_app.app.state
    user, token, _ = await state.gateway.principal(ctx.request)
    await state.gateway.capture_embedded(ctx.request, user, token, row, {
        "content": [{"type": "resource", "resource": {"uri": "fixture://temporary", "text": "body"}}],
    })

    async def forbidden(*args, **kwargs):
        raise AssertionError("Browse-all must not invoke semantic retrieval or start downstream")
    monkeypatch.setattr(state.embeddings, "score_candidates", forbidden)
    monkeypatch.setattr(state.embeddings, "scores", forbidden)
    monkeypatch.setattr(state.embeddings, "_embed", forbidden)
    monkeypatch.setattr(state.runtime, "connector", forbidden)
    configured = await db_app.web.patch("/api/v1/settings/embedding", json={
        "enabled": True, "base_url": "https://embedding.invalid/v1", "model": "test-model",
    })
    assert configured.status_code == 200

    async def collect(keyword, total=5, **filters):
        cursor, seen = None, []
        while True:
            result = await invoke(db_app, ctx, "gateway_search_resources", {
                "keyword": keyword, "limit": 2, **filters, **({"cursor": cursor} if cursor else {}),
            })
            assert not result.is_error, result
            page = result.structured_content
            assert page["total"] == total
            assert 0 < page["returned"] <= 2
            seen.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                return seen
            assert len(seen) < total

    expected = await collect("")
    for match in ("fuzzy", "exact", "keyword_only"):
        assert await collect("*", match=match) == expected
    filtered = await collect(" * ", total=1, kind="template", uri_prefix="fixture://sensor")
    assert [item["name"] for item in filtered] == ["sensor"]
    assert state.runtime.status() == []
