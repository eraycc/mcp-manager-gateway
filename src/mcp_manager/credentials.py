"""Credential ownership and sealed credential persistence."""
import copy
from typing import Literal

from sqlalchemy import delete, select

from .database import SystemSetting
from .runtime import GatewayError

OAUTH_TOKEN_FIELDS = {
    "access_token",
    "expires_at",
    "expires_in",
    "id_token",
    "refresh_token",
    "token_type",
}


def catalog_owner(isolation: str, user_id: str | None) -> str:
    """Return the canonical catalog/runtime owner for an isolation policy."""
    return user_id or "anonymous" if isolation == "user" else "service"


def config_isolation(config: dict) -> Literal["shared", "user"]:
    """Return OAuth configuration ownership, defaulting legacy rows to shared."""
    auth = config.get("auth", {})
    value = auth.get("config_isolation", "shared")
    if value not in {"shared", "user"}:
        raise ValueError("OAuth config_isolation must be shared or user")
    return value


def credential_owner(isolation: str, user_id: str | None) -> str:
    """Return an authenticated credential owner for an isolation policy."""
    owner = catalog_owner(isolation, user_id)
    if owner == "anonymous":
        raise GatewayError("auth_required", "Sign in before configuring personal credentials")
    return owner


class CredentialStore:
    """Store service and personal secrets outside the MCP service row."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.db = catalog.db

    @staticmethod
    def key(server_id: str, owner: str) -> str:
        return f"credential:{server_id}:{owner}"

    async def _load(self, session, server_id: str, owner: str) -> dict:
        item = await session.get(SystemSetting, self.key(server_id, owner))
        if not item or not item.value:
            return {}
        value = self.catalog.unseal(item.value)
        if not isinstance(value, dict):
            raise GatewayError("credential_invalid", "Stored MCP credential is invalid")
        return value

    async def load(self, server_id: str, owner: str, session=None) -> dict:
        """Load and decrypt one owner payload."""
        if session is not None:
            return await self._load(session, server_id, owner)
        async with self.db.session() as current:
            return await self._load(current, server_id, owner)

    async def write(self, session, server_id: str, owner: str, value: dict) -> None:
        """Write or remove one owner payload in the caller's transaction."""
        key = self.key(server_id, owner)
        item = await session.get(SystemSetting, key)
        if not value:
            if item:
                await session.delete(item)
            return
        sealed = self.catalog.seal(value)
        if item:
            item.value = sealed
        else:
            session.add(SystemSetting(key=key, value=sealed))

    async def owners(self, server_id: str, session=None) -> set[str]:
        """List credential owners for one exact MCP service."""
        prefix = f"credential:{server_id}:"

        async def read(current):
            rows = await current.scalars(
                select(SystemSetting).where(
                    SystemSetting.key.startswith(prefix, autoescape=True)
                )
            )
            return {
                item.key[len(prefix):]
                for item in rows
                if item.value and item.key[len(prefix):]
            }

        if session is not None:
            return await read(session)
        async with self.db.session() as current:
            return await read(current)

    async def delete_owner(
        self,
        session,
        owner: str,
        *,
        server_id: str | None = None,
    ) -> None:
        """Delete only credentials belonging to an exact owner."""
        statement = delete(SystemSetting).where(
            SystemSetting.key.startswith("credential:", autoescape=True),
            SystemSetting.key.endswith(":" + owner, autoescape=True),
        )
        if server_id is not None:
            statement = statement.where(
                SystemSetting.key.startswith(
                    f"credential:{server_id}:", autoescape=True
                )
            )
        await session.execute(statement)

    async def delete_server(self, session, server_id: str) -> None:
        """Delete new and legacy credentials for one exact MCP service."""
        await session.execute(
            delete(SystemSetting).where(
                SystemSetting.key.startswith(
                    f"credential:{server_id}:", autoescape=True
                )
            )
        )
        await session.execute(
            delete(SystemSetting).where(
                SystemSetting.key.startswith(f"oauth:{server_id}:", autoescape=True)
            )
        )

    async def save_oauth_token(
        self,
        session,
        row,
        user_id: str | None,
        value: dict,
    ) -> str:
        """Save an OAuth token under the owner selected by row isolation."""
        owner = credential_owner(row.isolation, user_id)
        payload = await self._load(session, row.id, owner)
        payload["oauth_token"] = copy.deepcopy(value)
        await self.write(session, row.id, owner, payload)
        return owner

    async def status(self, row, user_id: str | None) -> dict:
        """Return the profile-facing credential state and action."""
        config = self.catalog.unseal(row.config)
        auth = config.get("auth", {"type": "none"})
        auth_type = auth.get("type", "none")
        required = row.isolation == "user" and auth_type in {"bearer", "oauth"}
        result = {
            "required": required,
            "auth_type": auth_type,
            "isolation": row.isolation,
            "config_isolation": config_isolation(config) if auth_type == "oauth" else None,
            "state": "not_required",
            "action": None,
        }
        if not required:
            return result

        owner = credential_owner(row.isolation, user_id)
        personal = await self.load(row.id, owner)
        if auth_type == "bearer":
            configured = bool(personal.get("auth_config", {}).get("token"))
            if not configured:
                return result | {"state": "missing", "action": "configure_bearer"}
        else:
            ownership = config_isolation(config)
            config_payload = (
                personal if ownership == "user" else await self.load(row.id, "service")
            )
            if not config_payload.get("auth_config"):
                return result | {"state": "missing", "action": "configure_oauth"}
            if not personal.get("oauth_token", {}).get("access_token"):
                return result | {
                    "state": "pending_authorization",
                    "action": "authorize",
                }

        cache = self.catalog.cached(row, user_id)
        cache_status = cache.get("cache_status")
        if cache_status in {"refreshing", "empty"}:
            state = "discovering"
        elif cache_status == "ready":
            state = "ready"
        else:
            state = "failed"
        return result | {"state": state, "action": None}

    async def persist_config(
        self,
        session,
        row,
        full_config: dict,
        new_isolation: str,
        actor_id: str | None,
    ) -> dict:
        """Separate secrets from a service config and return its safe descriptor."""
        stored = copy.deepcopy(full_config)
        auth = stored.get("auth", {"type": "none"})
        auth_type = auth.get("type", "none")

        if auth_type == "bearer":
            owner = credential_owner(new_isolation, actor_id)
            payload = await self._load(session, row.id, owner)
            payload["auth_config"] = copy.deepcopy(auth)
            payload.pop("oauth_token", None)
            await self.write(session, row.id, owner, payload)
            if row.isolation in {"service", "session"} and new_isolation == "user":
                await session.flush()
                await self.write(session, row.id, "service", {})
            stored["auth"] = {"type": "bearer"}
            return stored

        if auth_type != "oauth":
            return stored

        ownership = config_isolation(stored)
        if new_isolation != "user" and ownership != "shared":
            raise ValueError("OAuth config_isolation must be shared outside user isolation")
        config_owner = (
            credential_owner(new_isolation, actor_id)
            if new_isolation == "user" and ownership == "user"
            else "service"
        )
        token_owner = credential_owner(new_isolation, actor_id)
        auth_config = {
            key: copy.deepcopy(value)
            for key, value in auth.items()
            if key not in OAUTH_TOKEN_FIELDS
            and key not in {"config_isolation", "granted_scope"}
        }
        oauth_token = {
            key: copy.deepcopy(value)
            for key, value in auth.items()
            if key in OAUTH_TOKEN_FIELDS
        }
        if "granted_scope" in auth:
            oauth_token["scope"] = copy.deepcopy(auth["granted_scope"])
        elif oauth_token and "scope" in auth:
            oauth_token["scope"] = copy.deepcopy(auth["scope"])
            auth_config.pop("scope", None)

        payloads = {
            owner: await self._load(session, row.id, owner)
            for owner in {config_owner, token_owner}
        }
        payloads[config_owner]["auth_config"] = auth_config
        if oauth_token:
            payloads[token_owner]["oauth_token"] = oauth_token
        if config_owner != token_owner:
            payloads[config_owner].pop("oauth_token", None)
            payloads[token_owner].pop("auth_config", None)
        for owner, payload in payloads.items():
            await self.write(session, row.id, owner, payload)
        if (
            row.isolation in {"service", "session"}
            and new_isolation == "user"
            and ownership == "user"
        ):
            await session.flush()
            await self.write(session, row.id, "service", {})

        stored["auth"] = {"type": "oauth", "config_isolation": ownership}
        return stored

    async def _materialize(self, row, user_id: str | None, session, require: bool) -> dict:
        config = self.catalog.unseal(row.config)
        auth = config.get("auth", {"type": "none"})
        auth_type = auth.get("type", "none")
        if auth_type not in {"bearer", "oauth"}:
            return config

        owner = credential_owner(row.isolation, user_id)
        if auth_type == "bearer":
            payload = await self._load(session, row.id, owner)
            resolved = payload.get("auth_config")
            if not resolved and auth.get("token"):
                resolved = auth
            if not resolved or not resolved.get("token"):
                if require:
                    raise GatewayError(
                        "auth_required",
                        "Bearer credential is not configured for this MCP service",
                    )
                return config
            config["auth"] = copy.deepcopy(resolved)
            return config

        ownership = config_isolation(config)
        config_owner = owner if row.isolation == "user" and ownership == "user" else "service"
        config_payload = await self._load(session, row.id, config_owner)
        token_payload = (
            config_payload
            if config_owner == owner
            else await self._load(session, row.id, owner)
        )
        auth_config = config_payload.get("auth_config")
        if not auth_config and any(
            key in auth for key in ("authorization_url", "token_url", "client_id")
        ):
            auth_config = {
                key: copy.deepcopy(value)
                for key, value in auth.items()
                if key not in OAUTH_TOKEN_FIELDS and key != "config_isolation"
            }
        oauth_token = token_payload.get("oauth_token")

        legacy_key = f"oauth:{row.id}:{owner}"
        legacy = await session.get(SystemSetting, legacy_key)
        if not oauth_token and legacy and legacy.value:
            oauth_token = self.catalog.unseal(legacy.value)
            token_payload["oauth_token"] = copy.deepcopy(oauth_token)
            await self.write(session, row.id, owner, token_payload)
            if auth_config and not config_payload.get("auth_config"):
                config_payload["auth_config"] = copy.deepcopy(auth_config)
                await self.write(session, row.id, config_owner, config_payload)
            await session.delete(legacy)

        if not auth_config:
            if require:
                raise GatewayError(
                    "auth_required",
                    "OAuth configuration is not available for this MCP service",
                )
            return config
        resolved = copy.deepcopy(auth_config)
        resolved["config_isolation"] = ownership
        if oauth_token:
            token_values = copy.deepcopy(oauth_token)
            granted_scope = token_values.pop("scope", None)
            resolved.update(token_values)
            if granted_scope is not None:
                resolved["granted_scope"] = granted_scope
        config["auth"] = resolved
        return config

    async def materialize(
        self,
        row,
        user_id: str | None,
        session=None,
        *,
        require: bool = True,
    ) -> dict:
        """Resolve the service descriptor with the requesting owner's secrets."""
        if session is not None:
            return await self._materialize(row, user_id, session, require)
        async with self.db.locked() as current:
            return await self._materialize(row, user_id, current, require)
