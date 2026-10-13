"""Catalog endpoints are summary-first and exact definitions are separate."""


def test_catalog_contract(api_paths):
    listing = api_paths["/api/v1/mcps/{server_id}/capabilities"]
    exact = api_paths["/api/v1/mcps/{server_id}/capabilities/{kind}/{name}"]
    assert "get" in listing and "get" in exact
    parameters = {item["name"] for item in listing["get"].get("parameters", [])}
    assert {"kind", "cursor", "limit", "q"} <= parameters
