"""Large log details expose a bounded preview and an authorized streaming download."""
import json


async def test_log_preview_is_bounded_and_download_preserves_full_record(db_app):
    marker = "large-result-" + "中" * 100_000
    identity = await db_app.app.state.logs.append({
        "user_id": db_app.actor["id"],
        "username": db_app.actor["username"],
        "mcp_id": "service",
        "tool_name": "large",
        "status": "success",
        "result": {"text": marker},
    })
    preview = await db_app.web.get(f"/api/v1/logs/{identity}/preview")
    assert preview.status_code == 200, preview.text
    assert len(preview.content) <= 70_000
    assert preview.json()["truncated"] is True
    assert len(preview.json()["preview"].encode("utf-8")) <= 65_536

    expected = await db_app.app.state.logs.detail(identity)
    download = await db_app.web.get(f"/api/v1/logs/{identity}/download")
    assert download.status_code == 200, download.text
    assert json.loads(download.content) == expected
