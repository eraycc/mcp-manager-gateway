"""Reservation growth is atomic across domains and failed growth keeps its old budget."""
import pytest
from test_storage_admission import prepare

from mcp_manager.runtime import GatewayError
from mcp_manager.storage_admission import StorageAdmission
from mcp_manager.storage_policy import MIB


def test_growth_preserves_other_writer_and_releases_original_on_failure(tmp_path):
    prepare(tmp_path, staging_bytes=2 * MIB, durable_reserve_bytes=MIB,
            min_free_bytes=0, min_free_percent=0)
    budget = StorageAdmission(tmp_path)
    try:
        with budget.reserve("cache", 1) as identity:
            budget.grow(identity, MIB)
            with budget.reserve("durable", MIB):
                with pytest.raises(GatewayError) as exc:
                    budget.grow(identity, MIB + 1)
                assert exc.value.code == "staging_capacity"
                assert budget.usage() == {"slots": 2, "bytes": 2 * MIB}
        assert budget.usage() == {"slots": 0, "bytes": 0}
        with pytest.raises(ValueError):
            budget.grow(identity, 1)
    finally:
        budget.close()
