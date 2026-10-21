"""Explicit OAuth modes validate active fields while legacy credentials remain compatible."""
import pytest

from mcp_manager.transports import validate_config


def remote(auth, transport="streamable-http"):
    validate_config(transport, {"url": "https://mcp.test/mcp", "auth": {"type": "oauth", **auth}})


def test_public_manual_client_needs_no_secret_or_fixed_endpoints():
    remote({"auth_mode": "manual", "client_id": "public"})


def test_dcr_accepts_a_name_without_manually_registered_credentials():
    remote({"auth_mode": "dcr", "client_name": "Codex"})


@pytest.mark.parametrize("auth", [
    {"auth_mode": "manual"}, {"auth_mode": "manual", "client_id": " "},
    {"auth_mode": "dcr", "client_name": ""}, {"auth_mode": "other", "client_id": "x"},
    {"auth_mode": "dcr", "client_name": "Codex", "client_id": "residual"},
    {"auth_mode": "manual", "client_id": "x", "client_name": "residual"},
    {"auth_mode": "manual", "client_id": "x", "token_endpoint_auth_method": "unknown"},
])
def test_invalid_or_residual_mode_fields_are_rejected(auth):
    with pytest.raises((ValueError, TypeError)):
        remote(auth)


def test_dcr_is_not_available_for_stdio_or_rest():
    for transport, config in (("stdio", {"command": "never"}), ("rest", {"tools": []})):
        with pytest.raises(ValueError, match="DCR"):
            validate_config(transport, config | {"auth": {"type": "oauth", "auth_mode": "dcr", "client_name": "Codex"}})


def test_proposals_accept_public_or_automatic_clients_without_secret():
    from mcp_manager.proposals import validate_proposal_oauth

    for config in ({"type": "oauth", "auth_mode": "manual", "client_id": "public"},
                   {"type": "oauth", "auth_mode": "auto", "allow_dcr": False},
                   {"type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex"}):
        validate_proposal_oauth(config)
        remote(config)
