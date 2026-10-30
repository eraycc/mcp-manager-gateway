"""A bounded durable append fence survives worker changes and projection loss."""
from uuid import UUID, uuid4

import pytest
from filelock import FileLock

from mcp_manager.log_epoch import LogEpoch


def open_epochs(tmp_path):
    return LogEpoch(tmp_path / "logs/calls", FileLock(str(tmp_path / "sources.lock")))


def test_two_workers_share_uuid_epoch_and_retirement_without_per_id_metadata(tmp_path):
    first, peer = open_epochs(tmp_path), open_epochs(tmp_path)
    identities = [first.new_id(), peer.new_id()]
    assert all(UUID(identity).version == 8 for identity in identities)
    assert first.birth(identities[0]) == peer.birth(identities[1]) == 1
    assert not peer.retired(identities[0])
    first.advance_after_verified_delete()
    assert all(peer.retired(identity) for identity in identities)
    fresh = peer.new_id()
    assert first.birth(fresh) == 2 and not first.retired(fresh)
    reopened = open_epochs(tmp_path)
    assert reopened.retired(identities[0]) and not reopened.retired(fresh)
    assert first.path.stat().st_size < 1024
    assert not list(first.path.parent.glob("*.tmp"))


def test_legacy_uuid_and_caller_ids_are_not_guessed_to_have_an_epoch(tmp_path):
    epochs = open_epochs(tmp_path)
    for identity in ("legacy-call", str(uuid4()), "", None):
        assert epochs.birth(identity) is None
        assert not epochs.retired(identity)


def test_corrupt_epoch_is_never_silently_reset(tmp_path):
    epochs = open_epochs(tmp_path)
    epochs.path.write_text('{"format":1,"current":0,"retired":5}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Invalid call epoch"):
        open_epochs(tmp_path)
