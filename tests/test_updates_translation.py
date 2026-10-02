"""Update detection and translation settings contracts."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.translation import DEFAULT_TRANSLATION_CONFIG, normalize_translation_config
from mcp_manager.updates import AUTO_CHECK_INTERVAL, is_newer, read_update_state, write_update_state


def test_version_comparison_and_translation_normalization():
    assert is_newer("1.0.11", "1.0.10")
    assert not is_newer("1.0.10", "1.0.10")
    assert not is_newer("1.0.9", "1.0.10")
    assert AUTO_CHECK_INTERVAL == timedelta(days=7)
    config = normalize_translation_config({
        "enabled": True,
        "local_language": "chinese_simplified",
        "target_language": "english",
        "service": "custom",
        "custom_host": "https://translate.example.com/",
        "sse_enabled": True,
        "ignore": {"class": ["notranslate"], "id": [], "tag": ["code"], "text": ["MCP"]},
        "terminology": [{"source": "网关", "target": "gateway"}],
        "url_control": True,
        "url_parameter": "language",
        "dynamic_content": True,
        "whole_page": True,
        "translate_local": False,
        "queue_enabled": True,
    })
    assert config["custom_host"] == "https://translate.example.com"
    assert config["sse_enabled"] is True
    assert config["ignore"]["tag"] == ["code"]
    assert normalize_translation_config({}) == DEFAULT_TRANSLATION_CONFIG
    with pytest.raises(ValueError):
        normalize_translation_config({"service": "custom", "custom_host": "file:///tmp/translate"})


@pytest.mark.asyncio
async def test_update_state_is_cached_ignored_and_force_refreshed(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path, database_url="", secret_key="update-test"))
    del app.state.protocol
    calls = []
    versions = iter(["9.9.9", "10.0.0", "10.1.0", "10.2.0"])

    async def fake_fetch():
        calls.append(datetime.now(UTC))
        version = next(versions)
        return {
            "version": version,
            "pypi_url": "https://pypi.org/project/mcp-manager-gateway/",
            "releases_url": "https://github.com/eraycc/mcp-manager-gateway/releases",
        }

    monkeypatch.setattr("mcp_manager.updates.fetch_latest_release", fake_fetch)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as web,
    ):
        credentials = {"username": "admin", "password": "password12345"}
        assert (await web.post("/api/v1/auth/register", json=credentials)).status_code == 200
        assert (await web.post("/api/v1/auth/login", json=credentials)).status_code == 200
        web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]

        about = (await web.get("/api/v1/about")).json()
        assert about["releases_url"].endswith("/releases")
        system_settings = (await web.get("/api/v1/settings")).json()
        assert system_settings["update_auto_check"] is True
        first = (await web.get("/api/v1/update-status")).json()
        assert first["latest_version"] == "9.9.9"
        assert first["update_available"] is True
        assert first["notification_count"] == 1
        assert (await read_update_state(app.state.db, credentials["username"])) == {}
        stored = await read_update_state(app.state.db, (await web.get("/api/v1/me")).json()["id"])
        assert stored["current_version"] == first["current_version"]
        assert len(calls) == 1

        cached = (await web.get("/api/v1/update-status")).json()
        assert cached["latest_version"] == "9.9.9"
        assert cached["auto_check_enabled"] is True
        assert len(calls) == 1

        disabled = await web.patch("/api/v1/settings", json={"update_auto_check": False})
        assert disabled.status_code == 200
        assert disabled.json()["update_auto_check"] is False
        paused = (await web.get("/api/v1/update-status")).json()
        assert paused["latest_version"] == "9.9.9"
        assert paused["auto_check_enabled"] is False
        assert len(calls) == 1

        ignored = (await web.post("/api/v1/update-status/ignore")).json()
        assert ignored["ignored_version"] == "9.9.9"
        assert ignored["update_available"] is False
        assert ignored["notification_count"] == 0

        refreshed = (await web.post("/api/v1/update-status/check")).json()
        assert refreshed["latest_version"] == "10.0.0"
        assert refreshed["update_available"] is True
        assert refreshed["auto_check_enabled"] is False
        assert len(calls) == 2

        assert (await web.patch("/api/v1/settings", json={"update_auto_check": True})).status_code == 200
        user_id = (await web.get("/api/v1/me")).json()["id"]
        expired_state = await read_update_state(app.state.db, user_id)
        expired_state["checked_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        await write_update_state(app.state.db, user_id, expired_state)
        expired = (await web.get("/api/v1/update-status")).json()
        assert expired["latest_version"] == "10.1.0"
        assert len(calls) == 3

        monkeypatch.setattr("mcp_manager.updates.VERSION", "10.1.0")
        after_local_upgrade = (await web.get("/api/v1/update-status")).json()
        assert after_local_upgrade["latest_version"] == "10.2.0"
        assert len(calls) == 4


@pytest.mark.asyncio
async def test_translation_settings_are_public_to_signed_in_users_and_admin_validated(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, database_url="", secret_key="translation-test"))
    del app.state.protocol
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as web,
    ):
        credentials = {"username": "admin", "password": "password12345"}
        await web.post("/api/v1/auth/register", json=credentials)
        await web.post("/api/v1/auth/login", json=credentials)
        web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]

        initial = (await web.get("/api/v1/translation/settings")).json()
        assert initial == DEFAULT_TRANSLATION_CONFIG

        configured = dict(DEFAULT_TRANSLATION_CONFIG) | {
            "enabled": True,
            "target_language": "japanese",
            "ignore": {"class": ["notranslate"], "id": [], "tag": [], "text": []},
        }
        saved = await web.patch("/api/v1/settings", json={"translation_config": configured})
        assert saved.status_code == 200
        assert (await web.get("/api/v1/translation/settings")).json()["target_language"] == "japanese"

        invalid = dict(configured) | {"service": "custom", "custom_host": "javascript:alert(1)"}
        response = await web.patch("/api/v1/settings", json={"translation_config": invalid})
        assert response.status_code == 422
