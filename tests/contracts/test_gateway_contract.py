"""Gateway discovery and lease operations keep their stable public surface."""


def test_gateway_contract(db_app, api_paths):
    assert {"post"} <= set(api_paths["/gateway/v1/leases"])
    assert {"delete"} <= set(api_paths["/gateway/v1/leases/{lease_id}"])
    status = db_app.app.state.runtime.capacity_status()
    assert {
        "active_business_calls",
        "queued_business_calls",
        "instances",
    } <= set(status)
