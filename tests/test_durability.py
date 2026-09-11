import asyncio

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.jobs import Jobs
from test_runtime import Fake


async def test_final_log_failure_preserves_successful_downstream_result(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path, secret_key="durability-test"))
    async with app.router.lifespan_context(app):
        fake = Fake()
        app.state.runtime.connector = fake.connect
        row = await app.state.catalog.create({"name": "fake", "transport": "stdio", "config": {"command": "fake"}})
        count = 0
        async def append(event):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("disk full")
            return event["id"]
        monkeypatch.setattr(app.state.logs, "append", append)
        lease = app.state.runtime.create_lease("u", "t")
        result = await app.state.catalog.call(row, lease, "echo", {"value": "success"})
        assert result["content"][0]["text"] == "success"
        assert fake.calls == 1


async def test_recovered_job_is_not_replayed(tmp_path):
    jobs = Jobs(tmp_path)
    entered = asyncio.Event()
    async def operation(item):
        entered.set()
        await asyncio.Event().wait()
    job = jobs.submit("write", [1], operation, "user")
    await entered.wait()
    recovered = Jobs(tmp_path)
    assert recovered.items[job["id"]]["status"] == "interrupted"
    assert recovered.tasks == {}
    await jobs.close()
