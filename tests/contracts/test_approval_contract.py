"""Approval remains human-controlled and exposes explicit test endpoints."""


def test_approval_contract(api_paths):
    proposal = api_paths["/api/v1/mcp-proposals/{proposal_id}"]
    assert {"get", "patch", "delete"} <= set(proposal)
    assert "post" in api_paths["/api/v1/mcp-proposals/{proposal_id}/test"]
    assert "post" in api_paths["/api/v1/mcp-proposals/{proposal_id}/approve"]
    assert "post" in api_paths["/api/v1/mcp-proposals/{proposal_id}/reject"]
