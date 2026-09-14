"""Administrator API for sealed semantic-discovery embedding settings."""
import math
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from .catalog import web_authorizer
from .database import SystemSetting
from .identity import admin_user

router = APIRouter(prefix="/api/v1/settings/embedding")
ADMIN = Depends(admin_user)
SETTING_KEY = "discovery_embedding"
REDACTED = "[REDACTED]"
DEFAULTS = {
    "enabled": False,
    "base_url": "",
    "model": "",
    "api_key": "",
    "timeout_seconds": 10.0,
    "min_similarity": 0.5,
}


def _valid_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate(value: dict) -> dict:
    if set(value) != set(DEFAULTS):
        raise HTTPException(422, "Invalid embedding settings")
    if not isinstance(value["enabled"], bool):
        raise HTTPException(422, "enabled must be a boolean")
    for key, limit in (("base_url", 2048), ("model", 256), ("api_key", 4096)):
        if not isinstance(value[key], str) or len(value[key]) > limit:
            raise HTTPException(422, key + " must be a string")
    base_url = value["base_url"].strip()
    model = value["model"].strip()
    if base_url:
        try:
            parsed = urlsplit(base_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment):
                raise ValueError
            _ = parsed.port
        except (ValueError, TypeError):
            raise HTTPException(422, "base_url must be an HTTP(S) API base or embeddings endpoint") from None
    if not _valid_number(value["timeout_seconds"]) or not 0 < float(value["timeout_seconds"]) <= 60:
        raise HTTPException(422, "timeout_seconds must be greater than 0 and at most 60")
    if not _valid_number(value["min_similarity"]) or not -1 <= float(value["min_similarity"]) <= 1:
        raise HTTPException(422, "min_similarity must be between -1 and 1")
    if value["enabled"] and (not base_url or not model):
        raise HTTPException(422, "Enabled embeddings require base_url and model")
    return {
        "enabled": value["enabled"],
        "base_url": base_url,
        "model": model,
        "api_key": value["api_key"],
        "timeout_seconds": float(value["timeout_seconds"]),
        "min_similarity": float(value["min_similarity"]),
    }


async def load_embedding_config(state) -> dict:
    async with state.db.session() as session:
        row = await session.get(SystemSetting, SETTING_KEY)
    if row is None:
        return dict(DEFAULTS)
    value = state.catalog.unseal(row.value)
    if not isinstance(value, dict):
        return dict(DEFAULTS)
    return dict(DEFAULTS) | {key: value[key] for key in DEFAULTS if key in value}


def _public(value: dict) -> dict:
    result = dict(value)
    result["api_key"] = REDACTED if result.get("api_key") else ""
    return result


@router.get("")
async def get_embedding_settings(request: Request, user=ADMIN):
    return _public(await load_embedding_config(request.app.state))


@router.patch("")
async def update_embedding_settings(data: dict, request: Request, user=ADMIN):
    if set(data) - set(DEFAULTS):
        raise HTTPException(422, "Unknown embedding setting")
    async with request.app.state.db.locked() as session:
        await web_authorizer(request)(session)
        row = await session.get(SystemSetting, SETTING_KEY)
        previous = dict(DEFAULTS) if row is None else (
            dict(DEFAULTS) | request.app.state.catalog.unseal(row.value)
        )
        candidate = dict(previous)
        for key, value in data.items():
            if key == "api_key" and value == REDACTED:
                continue
            candidate[key] = value
        candidate = _validate(candidate)
        sealed = request.app.state.catalog.seal(candidate)
        if row is None:
            session.add(SystemSetting(key=SETTING_KEY, value=sealed))
        else:
            row.value = sealed
    return _public(candidate)


@router.post("/test")
async def test_embedding_settings(request: Request, user=ADMIN):
    config = await load_embedding_config(request.app.state)
    await web_authorizer(request)()
    return await request.app.state.embeddings.probe(config)
