"""Credential ownership and sealed credential persistence."""
import copy
import time
from typing import Literal

from sqlalchemy import delete, or_, select

from .database import SystemSetting, User
from .runtime import GatewayError

STATIC_AUTH_FIELDS = {
    "bearer": ("token",),
    "basic": ("username", "password"),
    "api_key": ("value",),
}
PERSONAL_AUTH_TYPES = frozenset((*STATIC_AUTH_FIELDS, "oauth"))


def static_auth_configured(auth_type: str, auth: dict) -> bool:
    return auth.get("type") == auth_type and all(
        isinstance(auth.get(field), str) and bool(auth[field])
        for field in STATIC_AUTH_FIELDS[auth_type]
    )


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
        *,
        client: dict | None = None,
        renewal: bool = False,
    ) -> str:
        """Save an OAuth token under the owner selected by row isolation."""
        owner = credential_owner(row.isolation, user_id)
        before = await self.catalog._index_scope(session, row, owner) if renewal else None
        payload = await self._load(session, row.id, owner)
        payload["oauth_token"] = copy.deepcopy(value)
        if client is not None:
            payload["oauth_token_client"] = copy.deepcopy(client)
        payload["oauth_epoch"] = payload.get("oauth_epoch", 0) + 1
        payload.pop("oauth_state", None)
        await self.write(session, row.id, owner, payload)
        if renewal:
            await session.flush()
            after = await self.catalog._index_scope(session, row, owner)
            await self.catalog.retain_renewed_catalog(session, before, after)
        return owner

    async def configuration_summaries(self, rows, user_id: str) -> dict:
        """Aggregate personal credential readiness for an administrator's current page."""
        from .oauth import token_expiry
        from .oauth_config import callback_uri, registration_matches

        eligible = {row.id: row for row in rows
                    if row.isolation == "user" and row.auth_type in PERSONAL_AUTH_TYPES}
        if not eligible:
            return {}
        async with self.db.session() as session:
            users = (await session.execute(
                select(User.id, User.role, User.scope_mode, User.mcp_ids).where(User.disabled.is_(False))
            )).all()
            allowed = {
                identity: {user.id for user in users
                           if user.role == "admin" or user.scope_mode == "all" or identity in user.mcp_ids}
                for identity in eligible
            }
            settings = (await session.execute(
                select(SystemSetting.key, SystemSetting.value).where(or_(*[
                    SystemSetting.key.startswith(f"credential:{identity}:", autoescape=True)
                    for identity in eligible
                ]))
            )).all()
        payloads = {identity: {} for identity in eligible}
        for key, value in settings:
            _, identity, owner = key.split(":", 2)
            shared_oauth = (owner == "service" and eligible[identity].auth_type == "oauth"
                            and eligible[identity].config_isolation != "user")
            if value and (owner in allowed[identity] or shared_oauth):
                payloads[identity][owner] = self.catalog.unseal(value)

        checked_at = time.time()

        def configured(row, owner):
            personal = payloads[row.id].get(owner, {})
            if row.auth_type in STATIC_AUTH_FIELDS:
                return static_auth_configured(row.auth_type, personal.get("auth_config", {}))
            config_payload = personal if row.config_isolation == "user" else payloads[row.id].get("service", {})
            config = config_payload.get("auth_config", {})
            token = personal.get("oauth_token", {})
            registration = personal.get("oauth_token_client") or (
                personal.get("oauth_registration", {}) if personal.get("oauth_registration_private")
                else config_payload.get("oauth_registration", {}))
            # Resource changes clear registration in persist_config; listing never opens service configs.
            resource = registration.get("resource", "")
            registered = registration_matches(
                registration, config, resource, callback_uri(self.catalog.config.public_url, config))
            configured_client = (bool(registration.get("auth", {}).get("client_id")) if registered else
                all(isinstance(config.get(key), str) and config[key]
                    for key in ("authorization_url", "token_url", "client_id")))
            return bool(config.get("type") == "oauth" and configured_client and token.get("access_token")
                        and personal.get("oauth_state", {}).get("status") != "needs_reauth"
                        and (token.get("refresh_token") or token_expiry(token) > checked_at))

        return {
            identity: {
                "current_user_configured": user_id in allowed[identity] and configured(row, user_id),
                "configured_users": sum(configured(row, owner) for owner in allowed[identity]),
                "total_users": len(allowed[identity]),
            }
            for identity, row in eligible.items()
        }

    async def status(self, row, user_id: str | None, session=None) -> dict:
        """Return the profile-facing credential state and action."""
        config = self.catalog.unseal(row.config)
        auth = config.get("auth", {"type": "none"})
        auth_type = auth.get("type", "none")
        required = row.isolation == "user" and auth_type in PERSONAL_AUTH_TYPES
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
        personal = (await self._load(session, row.id, owner) if session is not None
                    else await self.load(row.id, owner))
        if auth_type in STATIC_AUTH_FIELDS:
            configured = static_auth_configured(auth_type, personal.get("auth_config", {}))
            if not configured:
                return result | {"state": "missing", "action": "configure_" + auth_type}
        else:
            ownership = config_isolation(config)
            config_payload = (
                personal if ownership == "user" else (
                    await self._load(session, row.id, "service") if session is not None
                    else await self.load(row.id, "service")
                )
            )
            if not config_payload.get("auth_config"):
                return result | {"state": "missing", "action": "configure_oauth"}
            from .oauth import oauth_auth_status

            auth_status = oauth_auth_status(personal)
            if personal.get("oauth_state", {}).get("status") == "needs_reauth":
                return result | {"state": "needs_reauth", "auth_status": auth_status, "action": "authorize"}
            if not personal.get("oauth_token", {}).get("access_token"):
                return result | {"state": "pending_authorization", "auth_status": auth_status, "action": "authorize"}
            if auth_status == "expired":
                return result | {"state": "expired", "auth_status": auth_status, "action": "authorize"}

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

        if auth_type == "bearer" or auth_type in STATIC_AUTH_FIELDS and new_isolation == "user":
            owner = credential_owner(new_isolation, actor_id)
            payload = await self._load(session, row.id, owner)
            payload["auth_config"] = copy.deepcopy(auth)
            payload.pop("oauth_token", None)
            await self.write(session, row.id, owner, payload)
            if row.isolation in {"service", "session"} and new_isolation == "user":
                await session.flush()
                await self.write(session, row.id, "service", {})
            stored["auth"] = {"type": auth_type}
            if auth_type == "api_key":
                stored["auth"]["header"] = auth.get("header", "X-API-Key")
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
        from .oauth_config import normalize_registration

        auth = normalize_registration(auth)
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
        elif oauth_token and "scope" in auth and auth["scope"] not in ("user", "service"):
            oauth_token["scope"] = copy.deepcopy(auth["scope"])
            auth_config.pop("scope", None)

        payloads = {
            owner: await self._load(session, row.id, owner)
            for owner in {config_owner, token_owner}
        }
        old_config = payloads[config_owner].get("auth_config")
        old_resource = self.catalog.unseal(row.config).get("url", "") if row.config else ""
        from .oauth_config import comparable_settings

        changed = (old_config is not None and comparable_settings(old_config) != comparable_settings(auth_config)) or bool(old_resource) and old_resource != full_config.get("url", "")
        if changed:
            affected = await self.owners(row.id, session=session) if config_owner == "service" else {token_owner}
            for owner in affected | {config_owner, token_owner}:
                item = payloads.setdefault(owner, await self._load(session, row.id, owner))
                item.pop("oauth_token", None)
                item.pop("oauth_token_client", None)
                item.pop("oauth_registration_private", None)
                item.pop("oauth_registration", None)
                item.pop("oauth_state", None)
                item["oauth_epoch"] = item.get("oauth_epoch", 0) + 1
            oauth_token = {}
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
        if auth_type not in PERSONAL_AUTH_TYPES or (auth_type in {"basic", "api_key"} and row.isolation != "user"):
            return config

        owner = credential_owner(row.isolation, user_id)
        if auth_type in STATIC_AUTH_FIELDS:
            payload = await self._load(session, row.id, owner)
            resolved = payload.get("auth_config") or {}
            if not resolved and auth_type == "bearer" and auth.get("token"):
                resolved = auth
            if not static_auth_configured(auth_type, resolved):
                if require:
                    label = {"bearer": "Bearer", "basic": "Basic", "api_key": "API Key"}[auth_type]
                    raise GatewayError(
                        "auth_required",
                        label + " credential is not configured for this MCP service",
                    )
                # Legacy inline Basic/API Key values have no provable personal owner.
                # Never return another user's inline secret as a restoration default.
                config["auth"] = {"type": auth_type}
                if auth_type == "api_key":
                    config["auth"]["header"] = auth.get("header", "X-API-Key")
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
