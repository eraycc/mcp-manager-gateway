from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import McpServer, User, set_setting
from test_bug1_catalog import console


async def test_schedule_expands_personal_oauth_only_for_authorized_owners(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="schedule-test"))
    async with app.router.lifespan_context(app):
        config = {"auth": {"type": "oauth", "scope": "user", "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"},
                  "tools": [{"name": "read", "request": {"url": "https://example.test"}}]}
        personal = await app.state.catalog.create({"name": "personal", "transport": "rest", "config": config})
        public = await app.state.catalog.create({"name": "public", "transport": "stdio", "config": {"command": "fake"}})
        async with app.state.db.locked() as session:
            # Seed enabled rows: this test exercises owner selection, not discovery.
            (await session.get(McpServer, personal.id)).mode = "lazy"
            (await session.get(McpServer, public.id)).mode = "lazy"
            session.add(User(id="allowed", username="allowed", password_hash="x", mcp_ids=[personal.id]))
            session.add(User(id="revoked", username="revoked", password_hash="x", mcp_ids=[]))
        for user_id in ("allowed", "revoked"):
            await set_setting(app.state.db, "oauth:" + personal.id + ":" + user_id,
                              app.state.catalog.seal({"access_token": user_id}))
        targets = await app.state.catalog.refresh_targets()
        assert {"server_id": personal.id, "user_id": "allowed"} in targets
        assert {"server_id": personal.id, "user_id": "revoked"} not in targets
        assert {"server_id": public.id, "user_id": None} in targets


async def test_startup_failure_threshold_setting_updates_live_catalog(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        settings = (await web.get("/api/v1/settings")).json()
        assert settings["startup_failure_threshold"] == 3
        updated = await web.patch(
            "/api/v1/settings", json={"startup_failure_threshold": 5}
        )
        assert updated.status_code == 200
        assert app.state.catalog.startup_failure_threshold == 5
        invalid = await web.patch(
            "/api/v1/settings", json={"startup_failure_threshold": 0}
        )
        assert invalid.status_code == 422
