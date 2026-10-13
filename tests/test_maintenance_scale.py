"""Maintenance identity checks scale with unique identities, not lease count."""
from sqlalchemy import event

from mcp_manager.app import batch_authorization_state
from mcp_manager.database import ApiToken, McpServer, User
from mcp_manager.runtime import Lease


async def test_many_leases_share_bounded_identity_queries(dual_db):
    async with dual_db.locked() as session:
        user = User(
            username="lease-user",
            password_hash="hash",
            role="user",
            scope_mode="selected",
            mcp_ids=[],
        )
        server = McpServer(
            name="Allowed",
            slug="allowed",
            transport="stdio",
            config={},
        )
        session.add_all([user, server])
        await session.flush()
        user.mcp_ids = [server.id]
        token = ApiToken(
            user_id=user.id,
            name="lease-token",
            prefix="prefix",
            token_hash="a" * 64,
            scope_mode="selected",
            mcp_ids=[server.id],
        )
        session.add(token)
        await session.flush()

    leases = [
        Lease(str(index), user.id, token.id, None, 0.0)
        for index in range(500)
    ]
    selects = 0

    def count_select(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    event.listen(dual_db.engine.sync_engine, "before_cursor_execute", count_select)
    try:
        states = await batch_authorization_state(dual_db, leases)
    finally:
        event.remove(dual_db.engine.sync_engine, "before_cursor_execute", count_select)

    assert selects <= 3
    assert len(states) == 500
    assert all(state == {server.id} for state in states.values())

    async with dual_db.locked() as session:
        current = await session.get(ApiToken, token.id)
        current.disabled = True
    revoked = await batch_authorization_state(dual_db, leases)
    assert all(state is None for state in revoked.values())
