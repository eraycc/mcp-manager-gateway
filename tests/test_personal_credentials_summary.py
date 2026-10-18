"""Admin capability summaries count credential readiness independently of discovery."""
import time

import pytest
from test_personal_capability_refresh import refresh_console

from mcp_manager.database import User
from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("auth_type", ["bearer", "basic", "api_key", "oauth", "oauth_shared"])
async def test_admin_summary_counts_only_enabled_authorized_configured_users(tmp_path, auth_type):
    async with refresh_console(tmp_path, auth_type=auth_type) as values:
        app, admin, reader_client, actor, reader, row, control = values
        store = app.state.credentials
        payload = await store.load(row.id, reader["id"])
        async with app.state.db.locked() as session:
            for identity, role, scope, ids, disabled in [
                ("other-admin", "admin", "selected", [], False),
                ("all-user", "user", "all", [], False),
                ("unassigned", "user", "selected", [], False),
                ("disabled-user", "user", "all", [], True),
            ]:
                session.add(User(id=identity, username=identity, password_hash="unused",
                                 role=role, scope_mode=scope, mcp_ids=ids, disabled=disabled))
            for identity in ("all-user", "unassigned", "disabled-user"):
                await store.write(session, row.id, identity, payload)
            incomplete = {"auth_config": {"type": row.auth_type}}
            await store.write(session, row.id, "other-admin", incomplete)
        # A failed discovery must not change whether a user's credentials are complete.
        control["fail"] = True
        with pytest.raises(GatewayError):
            await app.state.catalog.refresh_index(row.id, reader["id"])
        original = app.state.catalog.unseal

        def credential_only(sealed):
            assert sealed != row.config, "Service list must not decrypt connection configurations"
            return original(sealed)

        app.state.catalog.unseal = credential_only
        response = await admin.get("/api/v1/mcps")
        assert response.status_code == 200, response.text
        item = next(item for item in response.json()["items"] if item["id"] == row.id)
        assert item["personal_credentials"] == {
            "current_user_configured": True, "configured_users": 3, "total_users": 4,
        }
        assert control["connections"] == [reader["id"]]
        for secret in ("personal-token", "personal-password", "reader-access", "all-user", "other-admin"):
            assert secret not in response.text
        public = await reader_client.get("/api/v1/mcps")
        assert public.status_code == 200, public.text
        assert "personal_credentials" not in public.text
        app.state.catalog.unseal = original
        async with app.state.db.locked() as session:
            await store.write(session, row.id, actor["id"], {})
        refreshed = await admin.get("/api/v1/mcps")
        assert refreshed.json()["items"][0]["personal_credentials"] == {
            "current_user_configured": False, "configured_users": 2, "total_users": 4,
        }
        async with app.state.db.locked() as session:
            (await session.get(User, reader["id"])).mcp_ids = []
            (await session.get(User, "all-user")).disabled = True
        reduced = await admin.get("/api/v1/mcps")
        assert reduced.json()["items"][0]["personal_credentials"] == {
            "current_user_configured": False, "configured_users": 0, "total_users": 2,
        }


@pytest.mark.parametrize("case,expected", [
    ("valid", True), ("no_expiry", True), ("refreshable", True),
    ("expired", False), ("missing_token", False), ("incomplete_config", False),
])
@pytest.mark.parametrize("auth_type", ["oauth", "oauth_shared"])
async def test_oauth_summary_requires_complete_config_and_valid_authorization(tmp_path, case, expected, auth_type):
    async with refresh_console(tmp_path, auth_type=auth_type) as values:
        app, admin, _client, _actor, reader, row, _control = values
        async with app.state.db.locked() as session:
            payload = await app.state.credentials.load(row.id, reader["id"], session=session)
            token = {"access_token": "reader-access", "expires_at": time.time() + 3600}
            if case in {"expired", "refreshable"}:
                token["expires_at"] = time.time() - 3600
            if case == "refreshable":
                token["refresh_token"] = "renewal-secret"
            if case == "no_expiry":
                token.pop("expires_at")
            if case == "missing_token":
                token.pop("access_token")
            payload["oauth_token"] = token
            await app.state.credentials.write(session, row.id, reader["id"], payload)
            if case == "incomplete_config":
                owner = "service" if auth_type == "oauth_shared" else reader["id"]
                config = await app.state.credentials.load(row.id, owner, session=session)
                config["auth_config"] = {"type": "oauth", "client_id": "partial"}
                await app.state.credentials.write(session, row.id, owner, config)
        response = await admin.get("/api/v1/mcps")
        assert response.status_code == 200, response.text
        stats = response.json()["items"][0]["personal_credentials"]
        admin_configured = case != "incomplete_config" or auth_type != "oauth_shared"
        assert stats == {"current_user_configured": admin_configured,
                         "configured_users": int(admin_configured) + int(expected), "total_users": 2}
