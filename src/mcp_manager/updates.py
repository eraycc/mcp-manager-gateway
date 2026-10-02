"""Per-user PyPI update checks with persisted ignore state."""
import re
from datetime import UTC, datetime, timedelta

import httpx

from .about import VERSION
from .database import SystemSetting

PYPI_JSON_URL = "https://pypi.org/pypi/mcp-manager-gateway/json"
PYPI_PROJECT_URL = "https://pypi.org/project/mcp-manager-gateway/"
RELEASES_URL = "https://github.com/eraycc/mcp-manager-gateway/releases"
AUTO_CHECK_INTERVAL = timedelta(hours=6)


def _version_key(value):
    match = re.match(r"^\s*v?(\d+(?:\.\d+)*)(?:(?:[-_.]?)(dev|a|alpha|b|beta|rc|pre)(\d*)|.*)?\s*$",
                     str(value), re.IGNORECASE)
    if not match:
        return None
    release = tuple(int(part) for part in match.group(1).split("."))
    release = (release + (0,) * 8)[:8]
    label = (match.group(2) or "").lower()
    stage = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3, "pre": 3}.get(label, 4)
    number = int(match.group(3) or 0)
    return release + (stage, number)


def is_newer(candidate, current):
    candidate_key, current_key = _version_key(candidate), _version_key(current)
    return bool(candidate_key and current_key and candidate_key > current_key)


async def fetch_latest_release():
    async with httpx.AsyncClient(timeout=8, follow_redirects=True, trust_env=False) as client:
        response = await client.get(PYPI_JSON_URL, headers={"User-Agent": f"mcp-manager-gateway/{VERSION}"})
        response.raise_for_status()
        payload = response.json()
    version = str(payload.get("info", {}).get("version", "")).strip()
    if not _version_key(version):
        raise RuntimeError("PyPI returned an invalid version")
    return {"version": version, "pypi_url": PYPI_PROJECT_URL, "releases_url": RELEASES_URL}


def _checked_recently(state, now):
    try:
        checked = datetime.fromisoformat(state.get("checked_at", ""))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        return now - checked <= AUTO_CHECK_INTERVAL
    except (TypeError, ValueError):
        return False


def public_update_state(state=None, *, error=""):
    state = dict(state or {})
    latest = str(state.get("latest_version", ""))
    ignored = str(state.get("ignored_version", ""))
    available = is_newer(latest, VERSION) and latest != ignored
    return {
        "current_version": VERSION,
        "latest_version": latest,
        "ignored_version": ignored,
        "checked_at": state.get("checked_at"),
        "update_available": available,
        "notification_count": 1 if available else 0,
        "pypi_url": state.get("pypi_url", PYPI_PROJECT_URL),
        "releases_url": state.get("releases_url", RELEASES_URL),
        "check_error": error,
    }


async def read_update_state(db, user_id):
    key = "updates:" + user_id
    async with db.session() as session:
        row = await session.get(SystemSetting, key)
        return dict(row.value) if row and isinstance(row.value, dict) else {}


async def write_update_state(db, user_id, state):
    key = "updates:" + user_id
    async with db.locked() as session:
        row = await session.get(SystemSetting, key)
        if row:
            row.value = state
        else:
            session.add(SystemSetting(key=key, value=state))


async def cached_update_status(db, user_id):
    return public_update_state(await read_update_state(db, user_id))


async def update_status(db, user_id, *, force=False):
    state = await read_update_state(db, user_id)
    current = public_update_state(state)
    now = datetime.now(UTC)
    if not force and (current["update_available"] or _checked_recently(state, now)):
        return current
    try:
        release = await fetch_latest_release()
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return public_update_state(state, error=str(exc))
    state.update({
        "latest_version": release["version"],
        "pypi_url": release["pypi_url"],
        "releases_url": release["releases_url"],
        "checked_at": now.isoformat(),
    })
    await write_update_state(db, user_id, state)
    return public_update_state(state)


async def ignore_update(db, user_id):
    state = await read_update_state(db, user_id)
    if not state.get("latest_version"):
        return await update_status(db, user_id, force=True)
    state["ignored_version"] = state["latest_version"]
    await write_update_state(db, user_id, state)
    return public_update_state(state)
