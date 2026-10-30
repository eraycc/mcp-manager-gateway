import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_bug1_catalog import console
from test_discovery_failure_policy import Upstream

from mcp_manager.jsonl_store import JsonlStore


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


async def test_legacy_caches_consolidate_latest_revision_and_archive_json(tmp_path):
    from mcp_manager.config import Settings
    from mcp_manager.home_migration import reconcile_data
    async with console(tmp_path) as (app, _web, _actor):
        row = await app.state.catalog.create(
            {"name": "service", "config": {"command": "fake"}}, discover=False)
        server_id, revision = row.id, row.revision
        key = app.state.catalog.cache_key(row)
    cache = tmp_path / "cache"
    owner = key.rsplit("-", 1)[1]
    for version, name in [(revision, "current"), (0, "obsolete")]:
        (cache / f"{server_id}-{version}-{owner}.json").write_text(json.dumps({
            "tools": [{"name": name}], "cache_status": "ready"}), encoding="utf-8")
    report = await reconcile_data(Settings(data_dir=tmp_path, secret_key="bug1"))
    assert report["catalog_snapshots"] == 1
    async with console(tmp_path) as (app, _web, _actor):
        current = await app.state.catalog.get(server_id)
        assert (await app.state.catalog.tool_definition(current, "current"))["name"] == "current"
        assert await app.state.catalog.tool_definition(current, "obsolete") is None
        assert list(cache.glob("*.json")) == []
        assert not (cache / "catalog.jsonl").exists()
    assert len(list((tmp_path / "backups/reconcile").rglob("*.json"))) == 2

async def test_reconciliation_never_reads_or_removes_legacy_jobs(tmp_path, monkeypatch):
    from mcp_manager.config import Settings
    from mcp_manager.database import Database
    from mcp_manager.home_migration import reconcile_data
    from mcp_manager.job_models import JobRecord

    directory = tmp_path / "jobs"
    directory.mkdir()
    (directory / "one.json").write_text(json.dumps({
        "id": "one", "status": "running", "token": "secret-value", "completed": 1}), encoding="utf-8")
    settings = Settings(data_dir=tmp_path, secret_key="legacy-job-test")
    legacy = directory / "jobs.jsonl"
    legacy.write_text("opaque old job history", encoding="utf-8")
    original = JsonlStore.__init__

    def reject_jobs(store, path, **kwargs):
        assert Path(path).parent.name != "jobs", "Legacy jobs must never be read"
        original(store, path, **kwargs)

    monkeypatch.setattr(JsonlStore, "__init__", reject_jobs)
    assert (await reconcile_data(settings))["jobs"] == 0
    db = Database(settings)
    try:
        await db.initialize()
        async with db.session() as session:
            assert await session.get(JobRecord, "one") is None
    finally:
        await db.close()
    assert (directory / "one.json").exists()
    assert legacy.read_text(encoding="utf-8") == "opaque old job history"
    assert (await reconcile_data(settings))["jobs"] == 0


def test_unreadable_legacy_snapshot_is_preserved(tmp_path):
    legacy = tmp_path / "old.json"
    legacy.write_bytes(b"\xff\xfeprotected")
    store = JsonlStore(tmp_path / "state.jsonl")
    store.migrate_json(lambda path, value: ("old", value))
    assert legacy.exists()
    assert store.items == {}


async def test_missing_compatibility_cache_keeps_index_without_starting_lazy_process(tmp_path, monkeypatch):
    import asyncio

    from mcp_manager.app import create_app
    from mcp_manager.config import Settings
    async with console(tmp_path) as (app, _web, _actor):
        app.state.runtime.connector = Upstream().connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        app.state.catalog.delete_cache(row)
    peer = Upstream()
    monkeypatch.setattr("mcp_manager.app.connect", peer.connect)
    restarted = create_app(Settings(data_dir=tmp_path, secret_key="bug1"))
    async with restarted.router.lifespan_context(restarted):
        async with asyncio.timeout(5):
            while restarted.state.jobs.tasks:
                await asyncio.sleep(.01)
        current = await restarted.state.catalog.get(row.id)
        scope = await restarted.state.catalog.index_scope(current)
        assert current.mode == "lazy"
        assert (await restarted.state.catalog.index.get_summary(scope))["status"] == "ready"
        assert (await restarted.state.catalog.tool_definition(current, "first"))["name"] == "first"
        assert peer.starts == 0
        assert not restarted.state.runtime.instances

def test_delete_prefix_physically_removes_history_and_tombstones(tmp_path):
    store = JsonlStore(tmp_path / "state.jsonl", fail_closed=True)
    store.set("gone-user", {"payload": "x" * 4096})
    store.set("gone-user", {"payload": "latest"})
    store.set("gone-deleted", {"payload": "old"})
    store.set("gone-deleted", None)
    store.set("keep", {"payload": "valid"})
    before = store.path.stat().st_size
    store.delete_prefix("gone-")
    assert store.path.stat().st_size < before
    assert store.records == 1
    assert [json.loads(line)["key"] for line in store.path.read_text().splitlines()] == ["keep"]
    assert JsonlStore(store.path, fail_closed=True).items == {"keep": {"payload": "valid"}}
    first = store.path.read_bytes()
    store.compact()
    store.compact()
    assert store.path.read_bytes() == first


def test_delete_keys_is_exact_and_atomic_on_replace_failure(tmp_path, monkeypatch):
    store = JsonlStore(tmp_path / "state.jsonl", fail_closed=True)
    store.set("one", {"payload": "old"})
    store.set("one", {"payload": "current"})
    store.set("one-more", {"payload": "keep"})
    before = store.path.read_bytes()
    original_records = store.records

    def blocked(path, target):
        raise PermissionError("locked")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", blocked)
        import pytest
        with pytest.raises(PermissionError):
            store.delete_keys({"one"})
    assert store.path.read_bytes() == before
    assert store.get("one") == {"payload": "current"}
    assert store.records == original_records
    assert not list(tmp_path.glob("*.compact.jsonl"))
    assert store.delete_keys({"one"}) == 1
    assert JsonlStore(store.path).items == {"one-more": {"payload": "keep"}}
    assert store.delete_keys({"missing"}) == 0


def test_delete_prefix_never_resurrects_a_tombstoned_key(tmp_path):
    store = JsonlStore(tmp_path / "state.jsonl", fail_closed=True)
    store.set("old", {"payload": "obsolete"})
    store.set("old", None)
    store.set("keep", {"payload": "valid"})
    store.delete_prefix("unrelated-")
    assert JsonlStore(store.path, fail_closed=True).items == {"keep": {"payload": "valid"}}
    assert "obsolete" not in store.path.read_text()
