"""Administrator-only SQLite maintenance settings and background compaction."""
from fastapi import APIRouter, Depends, HTTPException, Request

from .catalog import web_authorizer
from .database import SystemSetting, invalidate_setting_cache
from .identity import admin_user
from .storage_maintenance import SETTING_KEY, normalize_policy, normalize_target

router = APIRouter(prefix="/api/v1/settings/storage")
ADMIN = Depends(admin_user)


def validate_target(state, target):
    if target not in {"main", "derived", "logs"}:
        raise HTTPException(404, "Database not found")
    if state.storage.path(target) is None:
        raise HTTPException(422, "Only file-backed SQLite databases support space reclamation")


@router.get("")
async def storage_settings(request: Request, user=ADMIN):
    return await request.app.state.storage.describe()


@router.patch("")
async def update_storage_settings(data: dict, request: Request, user=ADMIN):
    state = request.app.state
    if set(data) - {"main", "derived", "logs"}:
        raise HTTPException(422, "Unknown database")
    if "logs" in data and "derived" in data:
        raise HTTPException(422, "Specify derived or its legacy logs alias, not both")
    data = {normalize_target(target): value for target, value in data.items()}
    for target, value in data.items():
        validate_target(state, target)
        normalize_policy(value)
    async with state.db.locked() as session:
        user = await web_authorizer(request)(session)
        row = await session.get(SystemSetting, SETTING_KEY)
        values = dict(row.value) if row else {}
        for target, value in data.items():
            previous = values.get(target, values.get("logs", {}) if target == "derived" else {})
            values[target] = normalize_policy(previous | value)
        values.pop("logs", None)
        if row:
            row.value = values
        else:
            session.add(SystemSetting(key=SETTING_KEY, value=values))
    invalidate_setting_cache(state.db, [SETTING_KEY])
    await state.logs.audit("storage.settings.update", user.id, data)
    return await state.storage.describe()


@router.get("/governance")
async def storage_governance(request: Request, user=ADMIN):
    return await request.app.state.storage_governance.describe()


@router.patch("/governance")
async def update_governance(data: dict, request: Request, user=ADMIN):
    from .storage_policy import STORAGE_POLICY_KEY, normalize_storage_policy
    state = request.app.state
    async with state.db.locked() as session:
        user = await web_authorizer(request)(session)
        row = await session.get(SystemSetting, STORAGE_POLICY_KEY)
        previous = normalize_storage_policy(row.value if row else {})
        value = normalize_storage_policy(previous | data)
        if row:
            row.value = value
        else:
            session.add(SystemSetting(key=STORAGE_POLICY_KEY, value=value))
    invalidate_setting_cache(state.db, [STORAGE_POLICY_KEY])
    await state.storage_governance.refresh()
    changes = {key: {"old": previous[key], "new": value[key]} for key in data if previous[key] != value[key]}
    if changes:
        await state.logs.audit("storage.settings.update", user.id, {"changes": changes}, target_type="settings")
    return await state.storage_governance.describe()


@router.post("/governance/collect")
async def collect_expired_storage(request: Request, user=ADMIN):
    state = request.app.state
    user = await web_authorizer(request)()
    async def operation(_):
        await web_authorizer(request)()
        return await state.storage_governor.run_once(
            seconds=state.storage_governance.policy["gc_cycle_milliseconds"] / 1000,
            row_budget=state.storage_governance.policy["gc_row_budget"])
    return await state.jobs.submit("storage.collect", ["expired"], operation, user.id)


@router.post("/{target}/vacuum")
async def compact_database(target: str, request: Request, user=ADMIN):
    state = request.app.state
    target = normalize_target(target)
    validate_target(state, target)
    user = await web_authorizer(request)()
    if state.storage.locks[target].locked():
        raise HTTPException(409, "正在释放该数据库空间，请等待完成")
    async def operation(_):
        # The queued operation must revalidate the administrator before writing.
        await web_authorizer(request)()
        import asyncio
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            await web_authorizer(request)()
            result = await state.storage.run(target)
            if result.get("status") != "busy" or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(.1)
        await state.logs.audit("storage.vacuum", user.id, {"database": target, **result})
        return result
    return await state.jobs.submit("storage.vacuum", [target], operation, user.id)


@router.post("/governance/query-cache/clear")
async def clear_query_cache(request: Request, user=ADMIN):
    from .storage_manual import clear_query_caches
    state = request.app.state
    user = await web_authorizer(request)()
    async def operation(_):
        return await clear_query_caches(state, web_authorizer(request), user.id)
    return await state.jobs.submit("storage.query_cache.clear", ["query_cache"], operation, user.id)


@router.get("/governance/backup")
async def migration_backup(request: Request, user=ADMIN):
    from .storage_manual import current_backup
    return {"backup": await current_backup(request.app.state)}


@router.post("/governance/backup/{identity}/delete")
async def delete_migration_backup(identity: str, data: dict, request: Request, user=ADMIN):
    from .storage_manual import current_backup, delete_backup
    state = request.app.state
    user = await web_authorizer(request)()
    backup = await current_backup(state)
    if data != {"confirm": identity}:
        raise HTTPException(422, "Confirm the exact backup identity")
    if not backup or backup["id"] != identity or not backup["ready"]:
        raise HTTPException(409, "Only a completed migration backup can be deleted")
    async def operation(_):
        return await delete_backup(state, identity, web_authorizer(request), user.id)
    return await state.jobs.submit("storage.backup.delete", [identity], operation, user.id)
