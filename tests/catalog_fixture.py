"""Helpers that exercise the current immutable catalog rather than legacy JSONL."""
async def catalog_tools(app, row, user_id=None):
    scope = await app.state.catalog.index_scope(row, user_id)
    cursor, result = None, []
    while True:
        page = await app.state.catalog.index.list_capabilities(scope, kind="tools", cursor=cursor, limit=100)
        for item in page.items:
            result.append(await app.state.catalog.index.get_definition(scope, kind="tools", name=item["name"]))
        cursor = page.next_cursor
        if not cursor:
            return result


async def publish_tools(app, row, tools, user_id=None):
    scope = await app.state.catalog.index_scope(row, user_id)
    generation = await app.state.catalog.index.begin_refresh(scope)
    for start in range(0, len(tools), 100):
        await app.state.catalog.index.stage_page(generation.id, "tools", tools[start:start + 100])
    await app.state.catalog.index.publish(generation.id, expected_scope=scope)
