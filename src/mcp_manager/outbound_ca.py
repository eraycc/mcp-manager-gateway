"""One validated, atomically replaced CA bundle under the gateway home."""
import asyncio
import os
import ssl
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from . import outbound
from .catalog import web_authorizer
from .database import SystemSetting, invalidate_setting_cache

MAX_CA_BYTES = 1024 * 1024
CA_FILENAME = "proxy-ca.pem"


def settings_lock(state):
    if not hasattr(state, "outbound_settings_lock"):
        state.outbound_settings_lock = asyncio.Lock()
    return state.outbound_settings_lock


def managed_path(state):
    folder = state.config.home_dir / "credentials"
    target = folder / CA_FILENAME
    if folder.is_symlink() or target.is_symlink():
        raise HTTPException(409, "CA 凭据目录或文件不能是符号链接")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return target


def ca_metadata(value):
    if not value:
        return None
    path = Path(value)
    try:
        info = path.stat()
        available = path.is_file()
    except OSError:
        info, available = None, False
    return {
        "filename": path.name,
        "uploaded_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat() if available else None,
        "available": available,
    }


def stage_bytes(folder, content):
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".proxy-ca-", suffix=".tmp", delete=False) as stream:
            staged = Path(stream.name)
            stream.write(content)
        return staged
    except BaseException:
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise


def stage_ca(state, pem):
    if not isinstance(pem, str):
        raise HTTPException(422, "不是有效的 PEM CA 文件")
    try:
        content = pem.encode("utf-8")
    except UnicodeError:
        raise HTTPException(422, "不是有效的 PEM CA 文件") from None
    if len(content) > MAX_CA_BYTES:
        raise HTTPException(422, "CA 文件不能超过 1 MiB")
    if not content or "PRIVATE KEY" in pem:
        raise HTTPException(422, "不是有效的 PEM CA 文件")
    target = managed_path(state)
    staged = stage_bytes(target.parent, content)
    try:
        ssl.create_default_context(cafile=str(staged))
    except (ssl.SSLError, OSError, ValueError):
        staged.unlink(missing_ok=True)
        raise HTTPException(422, "不是有效的 PEM CA 文件") from None
    return staged


def restore_file(path, content, info):
    if content is None:
        path.unlink(missing_ok=True)
        return
    staged = stage_bytes(path.parent, content)
    try:
        os.utime(staged, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


async def persist_ca(request, staged):
    state = request.app.state
    async with settings_lock(state):
        target = managed_path(state)
        previous_bytes = target.read_bytes() if target.exists() else None
        previous_stat = target.stat() if previous_bytes is not None else None
        changed = False
        try:
            async with state.db.locked() as session:
                actor = await web_authorizer(request)(session)
                row = await session.get(SystemSetting, "outbound_proxy")
                previous = dict(outbound.DEFAULTS) if row is None else state.catalog.unseal(row.value)
                value = dict(previous, custom_ca_path=str(target) if staged else None,
                             version=previous["version"] + 1)
                if staged is None and value["ssl_verify"] == "custom_ca":
                    value["ssl_verify"] = "default"
                sealed = state.catalog.seal(value)
                if staged is not None:
                    os.replace(staged, target)
                    changed = True
                elif target.exists():
                    target.unlink()
                    changed = True
                if row is None:
                    session.add(SystemSetting(key="outbound_proxy", value=sealed))
                else:
                    row.value = sealed
        except BaseException:
            if changed:
                restore_file(target, previous_bytes, previous_stat)
            raise
        invalidate_setting_cache(state.db, ["outbound_proxy"])
        await state.logs.audit("settings.outbound_proxy.update", actor.id, {
            "mode": value["mode"], "version": value["version"], "changed_fields": ["custom_ca"],
            "reason_code": "ca_uploaded" if staged else "ca_removed",
        }, target_type="settings")
        return value
