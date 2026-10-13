"""Release-candidate deployment gates shared by SQLite and MySQL."""


async def test_release_candidate_meets_all_db_and_capacity_gates(db_app, db_backend):
    response = await db_app.web.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ready": True}
    dialect = db_app.db.engine.dialect.name
    assert dialect == ("sqlite" if db_backend == "sqlite" else "mysql")
    assert db_app.app.state.runtime.capacity.global_calls >= 64
    assert db_app.app.state.runtime.capacity.queued_calls >= 256
    assert db_app.app.state.runtime.capacity.global_instances >= 64
