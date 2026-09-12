import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp_manager.jsonl_store import JsonlStore
from mcp_manager.jobs import Jobs
from test_bug1_catalog import console
from test_discovery_failure_policy import Upstream


def test_compaction_and_restart_preserve_latest_values_in_one_file(tmp_path):
    path = tmp_path / "state.jsonl"
    store = JsonlStore(path)
    for index in range(300):
        store.set("one", {"index": index})
    assert store.records <= 128
    assert JsonlStore(path).get("one") == {"index": 299}
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_snapshots_and_delete_survive_restart(tmp_path):
    store = JsonlStore(tmp_path / "state.jsonl")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: store.set(str(i), {"index": i}), range(100)))
    store.set("42", None)
    reopened = JsonlStore(store.path)
    assert len(reopened.items) == 99
    assert reopened.get("42") is None


def test_torn_cache_write_does_not_resurrect_earlier_capabilities(tmp_path):
    path = tmp_path / "state.jsonl"
    JsonlStore(path).set("service", {"tools": [{"name": "old"}]})
    with path.open("ab") as stream:
        stream.write(b'{"key":"service","value":')
    assert JsonlStore(path, fail_closed=True).items == {}
    assert JsonlStore(path, fail_closed=True).items == {}


def test_compaction_lock_keeps_durable_append_and_cleans_staging(tmp_path, monkeypatch):
    store = JsonlStore(tmp_path / "state.jsonl")
    def blocked(path, target):
        raise PermissionError("compaction locked")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", blocked)
        for i in range(130):
            store.set("one", {"index": i})
    assert JsonlStore(store.path).get("one") == {"index": 129}
    assert not list(tmp_path.glob("*.compact.jsonl"))


async def test_legacy_caches_consolidate_latest_revision_and_remove_json(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        app.state.runtime.connector = Upstream().connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        server_id, revision = row.id, row.revision
        key = app.state.catalog.cache_key(row)
    cache = tmp_path / "cache"
    (cache / "catalog.jsonl").unlink()
    owner = key.rsplit("-", 1)[1]
    for version, name in [(revision, "current"), (0, "obsolete")]:
        (cache / f"{server_id}-{version}-{owner}.json").write_text(json.dumps({
            "tools": [{"name": name}], "cache_status": "ready"}), encoding="utf-8")
    async with console(tmp_path) as (app, web, actor):
        current = await app.state.catalog.get(server_id)
        assert app.state.catalog.cached(current)["tools"] == [{"name": "current"}]
        assert list(cache.glob("*.json")) == []
        assert [p.name for p in cache.iterdir()] == ["catalog.jsonl"]


def test_jobs_migrate_redact_and_mark_interrupted_without_replay(tmp_path):
    directory = tmp_path / "jobs"
    directory.mkdir()
    (directory / "one.json").write_text(json.dumps({
        "id": "one", "status": "running", "token": "secret-value", "completed": 1}), encoding="utf-8")
    jobs = Jobs(tmp_path)
    assert jobs.items["one"]["status"] == "interrupted"
    assert jobs.tasks == {}
    assert not list(directory.glob("*.json"))
    assert "secret-value" not in (directory / "jobs.jsonl").read_text(encoding="utf-8")
    assert Jobs(tmp_path).items["one"]["status"] == "interrupted"


def test_unreadable_legacy_snapshot_is_preserved(tmp_path):
    legacy = tmp_path / "old.json"
    legacy.write_bytes(b"\xff\xfeprotected")
    store = JsonlStore(tmp_path / "state.jsonl")
    store.migrate_json(lambda path, value: ("old", value))
    assert legacy.exists()
    assert store.items == {}


async def test_missing_lazy_cache_is_rebuilt_on_restart_without_keeping_process(tmp_path, monkeypatch):
    import asyncio
    from mcp_manager.app import create_app
    from mcp_manager.config import Settings
    async with console(tmp_path) as (app, web, actor):
        app.state.runtime.connector = Upstream().connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
    (tmp_path / "cache/catalog.jsonl").unlink()
    peer = Upstream()
    monkeypatch.setattr("mcp_manager.app.connect", peer.connect)
    restarted = create_app(Settings(data_dir=tmp_path, secret_key="bug1"))
    async with restarted.router.lifespan_context(restarted):
        async with asyncio.timeout(5):
            while restarted.state.jobs.tasks:
                await asyncio.sleep(.01)
        current = await restarted.state.catalog.get(row.id)
        assert current.mode == "lazy"
        assert restarted.state.catalog.cached(current)["cache_status"] == "ready"
        assert peer.starts == 1
        assert not restarted.state.runtime.instances
