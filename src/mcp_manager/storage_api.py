"""Administrator-only SQLite maintenance settings and background compaction."""
from fastapi import APIRouter, Depends, HTTPException, Request

from .catalog import web_authorizer
from .database import SystemSetting, invalidate_setting_cache
from .identity import admin_user
from .storage_maintenance import SETTING_KEY, normalize_policy

router = APIRouter(prefix="/api/v1/settings/storage")
ADMIN = Depends(admin_user)


def validate_target(state, target):
    if target not in {"main", "logs"}:
        raise HTTPException(404, "Database not found")
    if state.storage.path(target) is None:
        raise HTTPException(422, "Only file-backed SQLite databases support space reclamation")


@router.get("")
async def storage_settings(request: Request, user=ADMIN):
    return await request.app.state.storage.describe()


@router.patch("")
async def update_storage_settings(data: dict, request: Request, user=ADMIN):
    state = request.app.state
    if set(data) - {"main", "logs"}:
        raise HTTPException(422, "Unknown database")
    for target, value in data.items():
        validate_target(state, target)
        normalize_policy(value)
    async with state.db.locked() as session:
        user = await web_authorizer(request)(session)
        row = await session.get(SystemSetting, SETTING_KEY)
        values = dict(row.value) if row else {}
        for target, value in data.items():
            values[target] = normalize_policy(values.get(target, {}) | value)
        if row:
            row.value = values
        else:
            session.add(SystemSetting(key=SETTING_KEY, value=values))
    invalidate_setting_cache(state.db, [SETTING_KEY])
    await state.logs.audit("storage.settings.update", user.id, data)
    return await state.storage.describe()


@router.post("/{target}/vacuum")
async def compact_database(target: str, request: Request, user=ADMIN):
    state = request.app.state
    validate_target(state, target)
    user = await web_authorizer(request)()
    if state.storage.locks[target].locked():
        raise HTTPException(409, "正在释放该数据库空间，请等待完成")
    async def operation(_):
        # The queued operation must revalidate the administrator before writing.
        await web_authorizer(request)()
        result = await state.storage.run(target)
        await state.logs.audit("storage.vacuum", user.id, {"database": target, **result})
        return result
    return await state.jobs.submit("storage.vacuum", [target], operation, user.id)
