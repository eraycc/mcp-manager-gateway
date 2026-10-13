"""Identity endpoints retain their authenticated, bounded contract."""


async def test_identity_contract(db_app, api_paths):
    assert {"/api/v1/auth/login", "/api/v1/me", "/api/v1/tokens"} <= set(api_paths)
    response = await db_app.web.get("/api/v1/me")
    assert response.status_code == 200
    identity = response.json()
    assert identity["id"] == db_app.actor["id"]
    assert identity["role"] == "admin"
    assert len(response.content) <= 16_384
