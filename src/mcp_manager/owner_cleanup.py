"""Bounded authoritative retirement of directory scopes and orphan parent metadata."""
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, exists, select, text, update

from .catalog_control import CatalogControl
from .catalog_models import CatalogGeneration, CatalogHead, CatalogManifest
from .database import McpServer, User
from .derived_models import CatalogServer


def _user_owner(owner):
    if owner == "service":
        return False
    try:
        UUID(owner)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


async def prune_orphan_scopes(state, *, at, limit=100):
    if type(limit) is not int or not 3 <= limit <= 250:
        raise ValueError("Invalid owner cleanup budget")
    domains = ("owner-heads", "owner-controls", "owner-servers")
    part = limit // 3
    async with state.db.derived.session() as session:
        cursors = dict((await session.execute(text(
            "SELECT domain,position FROM storage_gc_cursors WHERE domain IN "
            "('owner-heads','owner-controls','owner-servers')"))).all())
        heads = (await session.execute(select(CatalogHead.scope_key, CatalogHead.server_id, CatalogHead.owner)
            .where(CatalogHead.scope_key > cursors.get(domains[0], "")).order_by(CatalogHead.scope_key)
            .limit(part))).all()
        parents = (await session.execute(select(CatalogServer.id).where(
            CatalogServer.id > cursors.get(domains[2], "")).order_by(CatalogServer.id)
            .limit(limit - part * 2))).all()
    async with state.db.session() as session:
        controls = (await session.execute(select(CatalogControl.scope_key, CatalogControl.server_id, CatalogControl.owner)
            .where(CatalogControl.scope_key > cursors.get(domains[1], "")).order_by(CatalogControl.scope_key)
            .limit(part))).all()
    candidates = {row.scope_key: row for row in (*heads, *controls)}
    server_ids = {row.server_id for row in candidates.values()} | {row.id for row in parents}
    deleted = 0
    async with state.db.locked() as authority:
        servers = dict((await authority.execute(select(McpServer.id, McpServer.isolation)
            .where(McpServer.id.in_(server_ids)))).all()) if server_ids else {}
        owners = {row.owner for row in candidates.values() if _user_owner(row.owner)}
        users = set((await authority.scalars(select(User.id).where(User.id.in_(owners)))).all()) if owners else set()
        invalid = []
        for key, row in candidates.items():
            isolation = servers.get(row.server_id)
            if (isolation is None
                    or row.owner == "service" and isolation == "user"
                    or _user_owner(row.owner) and (row.owner not in users or isolation != "user")):
                invalid.append(key)
        # Preserve unknown legacy ownership for explicit review, and leave every
        # body to the existing cache/durable root checks after metadata retirement.
        async with state.db.derived.locked() as derived:
            if invalid:
                await derived.execute(delete(CatalogHead).where(CatalogHead.scope_key.in_(invalid)))
                await derived.execute(update(CatalogManifest).where(CatalogManifest.scope_key.in_(invalid)).values(
                    current=False, retire_after=at - timedelta(microseconds=1)))
                await authority.execute(delete(CatalogControl).where(CatalogControl.scope_key.in_(invalid)))
                deleted = len(invalid)
            orphan_parents = {row.id for row in parents} - servers.keys()
            if orphan_parents:
                result = await derived.execute(delete(CatalogServer).where(
                    CatalogServer.id.in_(orphan_parents),
                    ~exists().where(CatalogGeneration.server_id == CatalogServer.id),
                    ~exists().where(CatalogHead.server_id == CatalogServer.id)))
                deleted += result.rowcount
            for domain, rows in zip(domains, (heads, controls, parents), strict=True):
                previous = (rows[-1].id if domain == "owner-servers" else rows[-1].scope_key) if rows else ""
                await derived.execute(text("""INSERT INTO storage_gc_cursors VALUES(:domain,:position,:at)
                    ON CONFLICT(domain) DO UPDATE SET position=excluded.position,updated_at=excluded.updated_at"""),
                    {"domain": domain, "position": previous, "at": at.timestamp()})
    return deleted
