"""Credential operations expose status in batches without exposing secrets."""


async def test_credentials_contract(db_app, api_paths):
    path = "/api/v1/mcps/credentials/status-batch"
    assert "post" in api_paths[path]
    response = await db_app.web.post(path, json={"ids": []})
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": []}
    assert "secret" not in response.text.lower()
