"""Administrator-only outbound settings; connectivity checks are advisory."""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import outbound, outbound_ca
from .catalog import web_authorizer
from .database import SystemSetting, invalidate_setting_cache
from .identity import admin_user

router = APIRouter(prefix="/api/v1/settings/outbound-proxy")
ADMIN = Depends(admin_user)


def public(value):
    result = dict(value)
    result["password"] = outbound.REDACTED if value.get("password") else ""
    result["custom_ca"] = outbound_ca.ca_metadata(result.pop("custom_ca_path", None))
    return result


def candidate(previous, data):
    value = dict(previous)
    if data.get("custom_ca_path") is not None:
        raise HTTPException(422, "请通过文件上传配置自定义 CA")
    for key, item in data.items():
        if key in {"custom_ca", "custom_ca_path"}:
            continue
        if key == "password" and item == outbound.REDACTED:
            continue
        value[key] = item
    if value["ssl_verify"] == "custom_ca" and not value.get("custom_ca_path"):
        raise HTTPException(422, "请先上传有效的 PEM CA 文件")
    return outbound.normalize_config(value)


@router.get("")
async def get_proxy_settings(request: Request, user=ADMIN):
    return public(await outbound.load_config(request.app.state))


@router.patch("")
async def update_proxy_settings(data: dict, request: Request, user=ADMIN):
    state = request.app.state
    async with outbound_ca.settings_lock(state), state.db.locked() as session:
        actor = await web_authorizer(request)(session)
        row = await session.get(SystemSetting, "outbound_proxy")
        previous = dict(outbound.DEFAULTS) if row is None else state.catalog.unseal(row.value)
        if "version" in data and data["version"] != previous["version"]:
            raise HTTPException(409, "代理配置已更新，请重新加载后重试")
        value = candidate(previous, data)
        value["version"] = previous["version"] + 1
        sealed = state.catalog.seal(value)
        if row is None:
            session.add(SystemSetting(key="outbound_proxy", value=sealed))
        else:
            row.value = sealed
    invalidate_setting_cache(state.db, ["outbound_proxy"])
    await state.logs.audit("settings.outbound_proxy.update", actor.id, {
        "mode": value["mode"], "version": value["version"],
        "changed_fields": [key for key in value if key != "version" and value[key] != previous[key]],
    }, target_type="settings")
    return public(value)


@router.post("/test")
async def test_proxy_connection(data: dict, request: Request, user=ADMIN):
    value = candidate(await outbound.load_config(request.app.state), data)
    await web_authorizer(request)()
    try:
        await outbound.validate_connection(value)
        result = {"ok": True, "detail": "连接测试通过（仅供参考）"}
    except outbound.ProxyDiagnosticError as exc:
        result = {"ok": False, "detail": str(exc) + "；此结果仅供参考，不影响保存"}
    except Exception as exc:  # noqa: BLE001 - diagnostics remain advisory and never expose raw errors
        # Environment proxy URLs and low-level exceptions may contain credentials.
        result = {"ok": False, "detail": f"连接测试未完成（{type(exc).__name__}）；仅供参考，不影响保存"}
    await web_authorizer(request)()
    return result


@router.post("/ca")
async def upload_proxy_ca(data: dict, request: Request, user=ADMIN):
    if set(data) != {"pem"}:
        raise HTTPException(422, "CA 上传只接受单个 PEM 文件")
    await web_authorizer(request)()
    staged = outbound_ca.stage_ca(request.app.state, data["pem"])
    try:
        return public(await outbound_ca.persist_ca(request, staged))
    finally:
        staged.unlink(missing_ok=True)


@router.delete("/ca")
async def remove_proxy_ca(request: Request, user=ADMIN):
    return public(await outbound_ca.persist_ca(request, None))
