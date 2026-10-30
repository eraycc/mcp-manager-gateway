"""Storage layout upgrades preserve independent authoritative log sources."""
import json
import sqlite3

import pytest

from mcp_manager.logs import LogStore


def event(identity, *, status="success"):
    return {"id": identity, "timestamp": "2026-10-30T01:00:00+00:00",
            "user_id": "owner", "tool_name": "echo", "status": status}


async def test_new_calls_use_subdirectory_and_keep_audit_separate(tmp_path):
    logs = LogStore(tmp_path)
    try:
        await logs.append(event("new"))
        await logs.audit("mcp.stop", None, {"reason": "idle"})
        assert (tmp_path / "logs/calls/2026-10-30.jsonl").is_file()
        assert not (tmp_path / "logs/2026-10-30.jsonl").exists()
        assert list((tmp_path / "logs/audit").glob("????-??-??.jsonl"))
        assert (tmp_path / "indexes/derived.sqlite").is_file()
        assert not (tmp_path / "indexes/logs.sqlite").exists()
        assert (await logs.detail("new"))["tool_name"] == "echo"
    finally:
        await logs.close()


async def test_legacy_and_new_same_day_sources_survive_rebuild_and_delete(tmp_path):
    source = tmp_path / "logs/2026-10-30.jsonl"
    source.parent.mkdir()
    source.write_text(json.dumps(event("legacy")) + "\n", encoding="utf-8")
    logs = LogStore(tmp_path)
    try:
        await logs.append(event("new"))
        await logs.rebuild()
        assert (await logs.query())["total"] == 2
        assert (await logs.detail("legacy"))["id"] == "legacy"
        assert (await logs.detail("new"))["id"] == "new"
        assert await logs.delete(["legacy"]) == 1
        await logs.rebuild()
        assert await logs.detail("legacy") is None
        assert (await logs.detail("new"))["id"] == "new"
        assert (tmp_path / "logs/calls/2026-10-30.jsonl").exists()
    finally:
        await logs.close()


def test_legacy_index_renames_only_after_all_connections_close(tmp_path):
    from mcp_manager.derived_index import prepare_index_path

    folder = tmp_path / "indexes"
    folder.mkdir()
    old = folder / "logs.sqlite"
    connection = sqlite3.connect(old)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE preserved(value TEXT)")
    connection.execute("INSERT INTO preserved VALUES('original')")
    connection.commit()
    try:
        with pytest.raises(RuntimeError, match="connection|busy|active"):
            prepare_index_path(tmp_path)
        assert old.exists()
        assert not (folder / "derived.sqlite").exists()
    finally:
        connection.close()
    renamed = prepare_index_path(tmp_path)
    assert renamed == folder / "derived.sqlite"
    assert not old.exists()
    with sqlite3.connect(renamed) as check:
        assert check.execute("SELECT value FROM preserved").fetchall() == [("original",)]


def test_conflicting_index_paths_are_never_overwritten(tmp_path):
    from mcp_manager.derived_index import prepare_index_path

    folder = tmp_path / "indexes"
    folder.mkdir()
    for name, value in (("logs.sqlite", "old"), ("derived.sqlite", "new")):
        with sqlite3.connect(folder / name) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker VALUES(?)", [value])
    with pytest.raises(RuntimeError, match="both|conflict"):
        prepare_index_path(tmp_path)
    for name, value in (("logs.sqlite", "old"), ("derived.sqlite", "new")):
        with sqlite3.connect(folder / name) as check:
            assert check.execute("SELECT value FROM marker").fetchone()[0] == value


async def test_call_rebuild_and_clear_preserve_other_index_domains(tmp_path):
    logs = LogStore(tmp_path)
    try:
        logs.conn.execute("CREATE TABLE catalog_test_marker(id INTEGER PRIMARY KEY)")
        logs.conn.execute("INSERT INTO catalog_test_marker VALUES(42)")
        logs.conn.commit()
        await logs.audit("mcp.stop", None, {})
        await logs.append(event("a"))
        await logs.rebuild()
        assert await logs.delete() == 1
        assert logs.conn.execute("SELECT id FROM catalog_test_marker").fetchone()[0] == 42
        assert logs.conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 1
        assert not list((tmp_path / "logs/calls").glob("????-??-??.jsonl"))
    finally:
        await logs.close()


async def test_another_live_log_writer_is_not_marked_unknown(tmp_path):
    first = LogStore(tmp_path)
    try:
        await first.append(event("active", status="running"))
        second = LogStore(tmp_path)
        try:
            await second.rebuild()
            assert (await first.detail("active"))["status"] == "running"
        finally:
            await second.close()
        await first.append(event("active", status="success"))
    finally:
        await first.close()
    reopened = LogStore(tmp_path)
    try:
        assert (await reopened.detail("active"))["status"] == "success"
    finally:
        await reopened.close()


async def test_old_source_generation_cannot_reproject_a_deleted_event(tmp_path):
    logs = LogStore(tmp_path)
    try:
        await logs.append(event("deleted"))
        source = tmp_path / "logs/calls/2026-10-30.jsonl"
        generation = logs.sources.generation(source)
        stale = LogStore(tmp_path)
        try:
            await logs.delete(["deleted"])
            # This writer has not seen the other writer's in-memory tombstones.
            assert stale._index(event("deleted"), source, 0, 1, source_generation=generation) is False
        finally:
            await stale.close()
        assert await logs.detail("deleted") is None
    finally:
        await logs.close()


async def test_legacy_large_payload_remains_available(tmp_path):
    root = tmp_path / "logs"
    payloads = root / "payloads"
    payloads.mkdir(parents=True)
    identity = "a" * 64
    body = event("legacy-large") | {"result": "valuable original result"}
    raw = json.dumps(body).encode()
    (payloads / (identity + ".json")).write_bytes(raw)
    record = event("legacy-large") | {"_payload_ref": {"id": identity, "size": len(raw)}}
    (root / "2026-10-30.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    logs = LogStore(tmp_path)
    try:
        assert (await logs.detail("legacy-large"))["result"] == "valuable original result"
        await logs.rebuild()
        assert (await logs.detail("legacy-large"))["result"] == "valuable original result"
    finally:
        await logs.close()


async def test_derived_maintenance_target_and_legacy_alias_share_policy(db_app):
    web, state = db_app.web, db_app.app.state
    response = await web.get("/api/v1/settings/storage")
    assert response.status_code == 200
    info = response.json()["databases"]["derived"]
    assert info["supported"] and info["stats"]["file_bytes"] > 0
    policy = {"startup_enabled": False, "min_free_mib": 48, "min_free_percent": 25}
    saved = await web.patch("/api/v1/settings/storage", json={"logs": policy})
    assert saved.status_code == 200
    assert saved.json()["databases"]["derived"]["policy"] == policy
    assert saved.json()["databases"]["logs"]["policy"] == policy
    assert state.storage.path("derived") == state.storage.path("logs")
    assert state.storage.path("derived").name == "derived.sqlite"
    assert state.storage.locks["logs"] is state.storage.locks["derived"]
    ambiguous = await web.patch("/api/v1/settings/storage", json={"logs": {}, "derived": {}})
    assert ambiguous.status_code == 422


async def test_a_stale_writer_cannot_append_a_tombstoned_call(tmp_path):
    first = LogStore(tmp_path)
    second = LogStore(tmp_path)
    try:
        await first.append(event("removed"))
        await first.delete(["removed"])
        await second.append(event("removed"))
        await second.rebuild()
        assert await second.detail("removed") is None
    finally:
        await second.close()
        await first.close()


def test_projection_programming_errors_fail_closed_and_release_resources(tmp_path, monkeypatch):
    source = tmp_path / "logs/calls/2026-10-30.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(event("must-not-be-skipped")) + "\n", encoding="utf-8")
    original = LogStore._index

    def broken(*args, **kwargs):
        raise TypeError("projection failure")

    monkeypatch.setattr(LogStore, "_index", broken)
    with pytest.raises(TypeError, match="projection failure"):
        LogStore(tmp_path)
    monkeypatch.setattr(LogStore, "_index", original)
    recovered = LogStore(tmp_path)
    try:
        assert recovered.conn.execute("SELECT id FROM calls").fetchone()[0] == "must-not-be-skipped"
    finally:
        recovered.conn.close()
        recovered.sources.close()
