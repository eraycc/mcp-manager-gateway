"""Catalog, sealed configuration and revision-scoped capability caches."""
import asyncio
import copy
import hashlib
import json
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from weakref import WeakValueDictionary

from cryptography.fernet import Fernet
from fastapi import HTTPException
from jsonschema import Draft202012Validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .artifact_store import ArtifactStore
from .catalog_operations import current_operation, tracked
from .catalog_store import CatalogStore, scope_key
from .catalog_types import CatalogScope
from .credentials import PERSONAL_AUTH_TYPES, catalog_owner, credential_owner
from .database import ApiToken, McpServer, SystemSetting, User, invalidate_setting_cache, now
from .db_types import strict_json_dumps
from .derived_cache import DerivedCache
from .diagnostics import SECRET_KEYS, safe_gateway_error
from .identity import public as public_model
from .identity import revalidate_user
from .runtime import GatewayError, ServerSpec
from .schema_validation import LOCAL_SCHEMA_REGISTRY, validate_local
from .storage_async import run_blocking
from .transports import validate_service_config

CAPABILITY_KEYS = ("tools", "resources", "prompts", "templates")
FAILURE_DEFAULTS = {
    "runtime_status": "stopped",
    "startup_failure_count": 0,
    "last_startup_error_code": None,
    "last_startup_error": None,
    "last_startup_failure_at": None,
    "failure_scope": "global",
    "last_refresh_error_code": None,
    "last_refresh_error": None,
    "last_refresh_failure_at": None,
    "failed_gateway_names": [],
    "cache_stale": False,
}


def masked(value, parent=""):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if (k.lower() in SECRET_KEYS or parent in {"env", "headers", "env_headers"})
                    and v else masked(v, k.lower())) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v, parent) for v in value]
    return value


def restore(value, previous):
    if value == "[REDACTED]":
        return previous
    if isinstance(value, dict):
        return {k: restore(v, previous.get(k) if isinstance(previous, dict) else None)
                for k, v in value.items()}
    if isinstance(value, list):
        return [restore(v, previous[i] if isinstance(previous, list) and i < len(previous) else None)
                for i, v in enumerate(value)]
    return value


def slug_base(name):
    """Build the stable, human-readable base used for generated service slugs."""
    value = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return (value or "mcp")[:64].rstrip("-") or "mcp"


def validate_slug(slug):
    if not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", slug) or "__" in slug:
        raise ValueError("Slug must contain 1–64 ASCII letters, digits, hyphens or single underscores")


def alias(slug, name):
    result = slug + "__" + name
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", result):
        return result
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", result)
    return safe[:47] + "_" + hashlib.sha256(result.encode()).hexdigest()[:16]



def web_authorizer(request, *, require_admin=True):
    """Authorize at a local write transaction or immediately before dispatch."""
    async def authorize(session=None):
        if session is not None:
            return await revalidate_user(request, session, require_admin=require_admin)
        async with request.app.state.db.locked() as locked:
            return await revalidate_user(request, locked, require_admin=require_admin)
    return authorize


class Catalog:
    def __init__(self, db, runtime, logs, config):
        self.db, self.runtime, self.logs, self.config = db, runtime, logs, config
        self.fernet = Fernet(config.encryption_key)
        self.cache_dir = Path(config.data_dir) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.locks = WeakValueDictionary()
        self.discovery_locks = WeakValueDictionary()
        self.oauth = None
        self.credentials = None
        self.tasks = set()
        self.on_cache_change = None
        self.cache_store = DerivedCache(config.data_dir, "catalog")
        self._cache_failures = {}
        self._health_views = OrderedDict()
        self._validators = OrderedDict()
        self._validator_bytes = 0
        try:
            self.artifacts = ArtifactStore(config.data_dir)
            self.index = CatalogStore(db, self.artifacts, signing_key=config.jwt_secret, scope_resolver=self._index_scope)
        except BaseException:
            if hasattr(self, "artifacts"):
                self.artifacts.close()
            self.cache_store.close()
            raise
        self.startup_failure_threshold = 3


    def schedule_warm(self, row, authorize=None, user_id=None):
        task = asyncio.create_task(self.warm(row.id, user_id, authorize=authorize, expected_revision=row.revision))
        self.tasks.add(task)
        def done(finished):
            self.tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()  # warm records failures in the audit stream
        task.add_done_callback(done)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.cache_store.close()
        self.artifacts.close()

    def seal(self, value):
        return {"sealed": self.fernet.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()}

    def unseal(self, value):
        return json.loads(self.fernet.decrypt(value["sealed"])) if "sealed" in value else copy.deepcopy(value)

    async def get(self, server_id, *, metadata_only=False):
        from sqlalchemy.orm import defer

        async with self.db.session() as s:
            row = await s.get(McpServer, server_id, options=[defer(McpServer.config)] if metadata_only else [])
        if not row:
            raise HTTPException(404, "MCP not found")
        return row

    async def rows(self, ids=None, *, metadata_only=False):
        from sqlalchemy.orm import defer

        statement = select(McpServer).order_by(McpServer.created_at.desc())
        if metadata_only:
            statement = statement.options(defer(McpServer.config))
        if ids is not None:
            statement = statement.where(McpServer.id.in_(ids))
        async with self.db.session() as s:
            return list((await s.scalars(statement)).all())

    def public(self, row, *, detail=False, user_id=None, runtime_user_id=None):
        result = {k: public_model(row)[k] for k in ("id", "slug", "name", "description", "tags", "transport",
                                             "mode", "isolation", "revision", "created_at")}
        cache = self.cached(row, user_id)
        runtime_filter = user_id if self.failure_scope(row) == "user" else runtime_user_id
        result.update(
            status=self.service_status(row, user_id),
            tool_count=cache.get("tool_count", len(cache["tools"])) if cache["cache_status"] == "ready" else 0,
            cache_at=cache.get("cache_at"),
            cache_error=cache.get("cache_error"),
            cache_status=cache.get("cache_status"),
            cache_error_code=cache.get("cache_error_code"),
            cache_attempt_at=cache.get("cache_attempt_at"),
            cache_stale=cache.get("cache_stale", False),
            auto_disabled=cache.get("auto_disabled", False),
            startup_failure_count=cache["startup_failure_count"],
            last_startup_error_code=cache["last_startup_error_code"],
            last_startup_error=cache["last_startup_error"],
            last_startup_failure_at=cache["last_startup_failure_at"],
            failure_scope=self.failure_scope(row),
            last_refresh_error_code=cache["last_refresh_error_code"],
            last_refresh_error=cache["last_refresh_error"],
            last_refresh_failure_at=cache["last_refresh_failure_at"],
            auth_type=self.unseal(row.config).get("auth", {}).get("type", "none"),
            runtime=[x for x in self.runtime.status(user_id=runtime_filter) if x["server_id"] == row.id],
        )
        if detail:
            result["config"] = masked(self.unseal(row.config))
        return result

    async def detail(self, row, user_id=None, *, reveal=False):
        """Return an editor model resolved only for the current owner."""
        result = self.public(row, user_id=user_id)
        config = await self.credentials.materialize(
            row, user_id, require=False
        )
        result["config"] = config if reveal else masked(config)
        return result

    def cache_key(self, row, user_id=None):
        owner = catalog_owner(row.isolation, user_id)
        suffix = hashlib.sha256(owner.encode()).hexdigest()[:24]
        return row.id + "-" + suffix

    def failure_scope(self, row):
        return "user" if row.isolation == "user" else "global"

    def service_status(self, row, user_id=None):
        if row.mode == "disabled":
            return "stopped"
        if self.cached_view(row, user_id)["runtime_status"] == "failed":
            return "failed"
        runtime_user_id = user_id if self.failure_scope(row) == "user" else None
        instances = [
            item for item in self.runtime.status(user_id=runtime_user_id)
            if item["server_id"] == row.id
        ]
        if any(item["in_flight"] > 0 for item in instances):
            return "running"
        if any(item["phase"] == "ready" for item in instances):
            return "ready"
        return "failed" if self.cached_view(row, user_id)["runtime_status"] == "failed" else "stopped"

    def failure_details(self, row, user_id=None):
        cache = self.cached_view(row, user_id)
        return {
            "mcp_id": row.id,
            "mcp_name": row.name,
            "status": self.service_status(row, user_id),
            "startup_failure_count": cache["startup_failure_count"],
            "failure_reason": cache["cache_error"] or cache["last_refresh_error"] or cache["last_startup_error"],
            "failure_scope": self.failure_scope(row),
            "recovery": "Ask an administrator to repair or refresh this MCP service.",
        }

    def cached(self, row, user_id=None):
        return copy.deepcopy(self.cached_view(row, user_id))

    def cached_view(self, row, user_id=None):
        """Internal read-only view; callers must not mutate its nested collections."""
        def current(key):
            saved = self._cache_failures.get(key) or self.cache_store.items.get(key)
            return saved["data"] if saved and saved["revision"] == row.revision else None
        saved = current(self.cache_key(row, user_id))
        empty = {
            **{key: [] for key in CAPABILITY_KEYS},
            "cache_at": None,
            "cache_error": None,
            "cache_error_code": None,
            "cache_status": "empty",
            "cache_attempt_at": None,
            "auto_disabled": False,
            **FAILURE_DEFAULTS,
        }
        if self.runtime.versions.get(row.id, row.revision) != row.revision:
            return empty | {"cache_status": "disabled"}
        value = empty | (saved or {})
        owner_scope = CatalogScope(row.id, catalog_owner(row.isolation, user_id), row.revision, "")
        control = self._health_views.get(scope_key(owner_scope))
        if control and control[0].revision == row.revision:
            health = control[1]
            refresh_warning = {key: value.get(key) for key in (
                "last_refresh_error", "last_refresh_error_code", "last_refresh_failure_at")}
            value.update(FAILURE_DEFAULTS, cache_error=None, cache_error_code=None, cache_stale=False)
            value.update(refresh_warning)
            value.update({key: item for key, item in health.items() if key not in {"invalidated", "failure_sequence"}})
            if health.get("invalidated"):
                value.update(cache_status="error", cache_at=None,
                             **{key[:-1] + "_count": 0 for key in CAPABILITY_KEYS})
        value["failure_scope"] = self.failure_scope(row)
        if value["cache_status"] == "empty" and value["cache_at"]:
            value["cache_status"] = "error" if value["cache_error"] else "ready"
        if row.mode == "disabled" or value["cache_status"] != "ready":
            value = value | {key: [] for key in CAPABILITY_KEYS}
            if row.mode == "disabled" and value["cache_status"] != "error":
                value["cache_status"] = "disabled"
        return value

    def delete_cache(self, row, user_id=None):
        """Delete one canonical owner cache without crossing isolation scopes."""
        key = self.cache_key(row, user_id)
        self.cache_store.set(key, None)
        self._cache_failures.pop(key, None)

    def save_cache(self, row, value, user_id=None, *, content_changed=True):
        value = dict(value)
        with self.cache_store.gate.operation(), self.cache_store.lock:
            has_index = self.cache_store.connection.execute(
                "SELECT 1 FROM catalog_heads WHERE server_id=? AND owner=? LIMIT 1",
                [row.id, catalog_owner(row.isolation, user_id)]).fetchone() is not None
        for kind in CAPABILITY_KEYS:
            value.setdefault(kind[:-1] + "_count", len(value.get(kind, [])))
            if has_index:
                value[kind] = []
        generation = uuid4().hex if content_changed else value.get("cache_generation") or uuid4().hex
        value = value | {"cache_generation": generation}
        key = self.cache_key(row, user_id)
        try:
            self.cache_store.set(key, {"revision": row.revision, "data": value},
                                 ttl_seconds=86400 if not has_index else None)
        except OSError as exc:
            error = GatewayError("cache_write_failed", "Local MCP cache write failed: " + str(exc))
            # Fail closed even when the disk cannot record the failure. Disabled
            # policy is committed separately, so restart cannot resurrect tools.
            failure = value | {key: [] for key in ("tools", "resources", "prompts", "templates")}
            failure.update(cache_status="error", cache_at=None)
            if not failure.get("cache_error"):
                failure.update(cache_error=str(error), cache_error_code=error.code,
                               cache_attempt_at=now().isoformat())
            self._cache_failures[key] = {"revision": row.revision, "data": failure}
            raise error from exc
        self._cache_failures.pop(key, None)

    async def begin_operation(self, scope):
        from .control_results import begin_operation
        return await begin_operation(self, scope)

    def _remember_health(self, scope, health):
        key = scope_key(scope)
        self._health_views[key] = (scope, dict(health))
        self._health_views.move_to_end(key)
        while len(self._health_views) > 2048:
            self._health_views.popitem(last=False)

    async def record_operation_result(self, row, user_id=None, **kwargs):
        from .control_results import record_result
        return await record_result(self, row, user_id, **kwargs)

    def invalidate_authorization_cache(self, row, error, user_id=None):
        """Explicit revocation or account/client changes invalidate the old authorization immediately."""
        value = self.cached(row, user_id)
        value.update(**{key: [] for key in CAPABILITY_KEYS},
                     **{key: 0 for key in ("tool_count", "resource_count", "prompt_count", "template_count")},
                     cache_status="auth_required", cache_at=None, cache_stale=False,
                     cache_error=str(error), cache_error_code=error.code)
        self.save_cache(row, value, user_id)

    async def record_start_success(self, row, user_id=None):
        await self.record_operation_result(row, user_id)
        return self.cached(row, user_id)

    async def record_start_failure(self, row, exc, user_id=None):
        await self.record_operation_result(row, user_id, error=exc, kind="startup")
        exc.startup_failure_recorded = True
        return exc

    async def record_refresh_failure(self, row, exc, user_id=None, *, had_ready,
                                     scope=None, expected_refresh=None):
        config = await self.credentials.materialize(row, user_id, require=False)
        exc = safe_gateway_error(exc, config=config)
        outcome = current_operation.get()
        if outcome is not None:
            outcome.refresh_version = getattr(exc, "catalog_refresh_id", expected_refresh)
        else:
            await self.record_operation_result(row, user_id, error=exc, kind="refresh",
                                               scope=scope, expected_refresh=expected_refresh)
        return exc

    async def retain_renewed_catalog(self, session, before, after):
        await self.index.rebind_renewed_credential(session, before, after)
        outcome = current_operation.get()
        if outcome is not None and outcome.scope == before:
            outcome.scope = after

    def mark_refreshing(self, row, user_id=None):
        if row.mode != "disabled" and self.runtime.versions.get(row.id, row.revision) == row.revision:
            value = self.cached(row, user_id)
            value.update(cache_status="ready" if value["cache_status"] == "ready" else "refreshing",
                         cache_attempt_at=now().isoformat())
            self.save_cache(row, value, user_id)

    async def connection_starting(self, spec):
        if spec.id.startswith("diagnose-"):
            return
        row = await self.get(spec.id)
        if row.revision != spec.revision or row.mode == "disabled":
            raise GatewayError("revision_changed", "MCP configuration changed before startup")

    async def start_failed(self, spec, exc):
        if spec.id.startswith("diagnose-"):
            return
        try:
            row = await self.get(spec.id)
        except HTTPException as missing:
            if missing.status_code == 404:
                return
            raise
        if row.revision == spec.revision and current_operation.get() is None:
            user_id = spec.credential_owner if spec.credential_owner not in {"", "service"} else None
            exc = safe_gateway_error(exc, config=spec.config)
            failure = await self.record_start_failure(row, exc, user_id)
            if failure is not exc and isinstance(failure, GatewayError):
                exc.code = failure.code
                exc.details.update(failure.details)

    async def store_discovery(self, row, result, user_id=None, *, spec=None):
        current = await self.get(row.id)
        if current.revision != row.revision or current.mode == "disabled":
            raise GatewayError("revision_changed", "MCP configuration changed during discovery")
        if spec is not None:
            self.runtime.mark_available(spec)
        result = result | {
            "cache_at": now().isoformat(),
            "cache_attempt_at": now().isoformat(),
            "cache_error": None,
            "cache_error_code": None,
            "cache_status": "ready",
            "runtime_status": "stopped",
            "startup_failure_count": 0,
            "last_startup_error_code": None,
            "last_startup_error": None,
            "last_startup_failure_at": None,
            "failure_scope": self.failure_scope(current),
            "last_refresh_error_code": None,
            "last_refresh_error": None,
            "last_refresh_failure_at": None,
            "failed_gateway_names": [],
        }
        scope = await self.index_scope(current, user_id)
        generation = await self.index.begin_refresh(scope)
        try:
            for kind in CAPABILITY_KEYS:
                entries = result.get(kind, [])
                for offset in range(0, len(entries), 250):
                    await self.index.stage_page(generation.id, kind, entries[offset:offset + 250])
            await self.index.set_metadata(generation.id, {
                key: result.get(key) for key in ("serverInfo", "protocolVersion", "capability_errors")
            })
            await self.index.publish(generation.id, expected_scope=scope)
        except BaseException:
            await asyncio.shield(self.index.fail_refresh(generation.id))
            raise
        try:
            self._remember_health(scope, {})
            await run_blocking(self.save_cache, current, result, user_id)
        except GatewayError as exc:
            exc.catalog_refresh_id = generation.id
            raise
        return result

    async def connection_ready(self, spec, connection):
        if spec.id.startswith("diagnose-"):
            return
        row = await self.get(spec.id)
        if row.revision != spec.revision:
            raise GatewayError("revision_changed", "MCP configuration changed during startup")

    async def refresh_after_change(self, row, user_id=None, authorize=None):
        if row.mode == "disabled":
            await run_blocking(self.save_cache, row, {"tools": [], "resources": [], "prompts": [], "templates": [],
                                  "cache_status": "disabled"}, user_id)
            return
        try:
            await self.refresh_index(row.id, user_id, authorize=authorize)
        except Exception:  # noqa: BLE001,S110 - remote failure is persisted by refresh/warm
            # Saving valid configuration succeeds even when a remote service is
            # offline; the attempt and actionable status are recorded in its cache.
            pass

    async def spec(self, row, user_id=None, *, with_scope=False):
        if row.mode == "disabled":
            raise GatewayError("disabled", "MCP is disabled; repair configuration or authorization, then enable it")

        def build(config, epoch=None):
            auth = config.get("auth", {})
            owner = credential_owner(row.isolation, user_id) if auth.get("type") in PERSONAL_AUTH_TYPES else ""
            binding = auth if epoch is None else {"auth": auth, "oauth_epoch": epoch}
            scope = hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16] if owner else ""
            if owner:
                self.runtime.credential_versions[(row.id, owner)] = scope
            return ServerSpec(row.id, row.transport, config, row.mode, row.isolation, row.revision, scope, owner, epoch,
                              name=row.name)

        config = self.unseal(row.config) if self.credentials is None else await self.credentials.materialize(row, user_id)
        auth = config.get("auth", {})
        if auth.get("type") == "oauth" and self.oauth is not None:
            from .oauth import token_from_auth

            value, epoch = await self.oauth.credentials(row, user_id, with_epoch=True)
            async with self.db.locked() as session:
                server = await session.get(McpServer, row.id)
                owner = credential_owner(row.isolation, user_id)
                payload = await self.credentials.load(row.id, owner, session=session)
                config = await self.credentials.materialize(row, user_id, session=session)
                if (not server or server.revision != row.revision
                        or payload.get("oauth_epoch", 0) != epoch
                        or payload.get("oauth_state", {}).get("status") == "needs_reauth"
                        or token_from_auth(config.get("auth", {})) != value):
                    raise GatewayError("credential_changed", "OAuth authorization changed; retry after reauthorization")
                from .environment import connection_config

                # Freeze expanded destinations before a queued call can observe changed environment variables.
                config = connection_config(config)
                config_payload = await self.credentials.load(row.id, self.oauth.config_owner(row, user_id), session=session)
                registration = config_payload.get("oauth_registration", {})
                if registration:
                    from .oauth_config import callback_uri, registration_fingerprint
                    from .oauth_discovery import canonical_resource

                    resource = config.get("url", "")
                    if resource and row.transport in {"streamable-http", "sse"}:
                        resource = canonical_resource(resource)
                    fingerprint = registration_fingerprint(
                        config.get("auth", {}), resource, callback_uri(self.config.public_url, config.get("auth", {})))
                    if registration.get("config_fingerprint") != fingerprint:
                        raise GatewayError("credential_changed", "OAuth resource changed while preparing the connection")
                prepared = build(config, epoch)
                if with_scope:
                    scope = await self._index_scope(session, server, catalog_owner(server.isolation, user_id))
                    return prepared, scope
                return prepared
        if auth.get("type") == "oauth" and not auth.get("access_token"):
            raise GatewayError("auth_required", "Complete OAuth authorization in the Web console")
        if with_scope:
            from sqlalchemy.orm import load_only

            async with self.db.locked() as session:
                server = await session.get(McpServer, row.id, options=[load_only(
                    McpServer.id, McpServer.revision, McpServer.isolation, McpServer.mode,
                )])
                if not server or server.revision != row.revision:
                    raise GatewayError("revision_changed", "MCP configuration changed during preparation")
                config = (self.unseal(row.config) if self.credentials is None else
                          await self.credentials.materialize(row, user_id, session=session))
                return build(config), await self._index_scope(
                    session, server, catalog_owner(server.isolation, user_id))
        return build(config)

    async def prepare_scope(self, row, user_id=None, *, authorize=None, retry=True):
        """Renew credentials, then atomically materialize the connection and catalog scope."""
        for attempt in range(2 if retry else 1):
            try:
                if authorize:
                    await authorize()
                prepared = await self.spec(row, user_id, with_scope=True)
                outcome = current_operation.get()
                if outcome is not None and outcome.row is not None and outcome.row.id == row.id:
                    outcome.scope = prepared[1]
                if authorize:
                    await authorize()
                return prepared
            except GatewayError as exc:
                if exc.code != "credential_changed" or attempt or not retry:
                    raise
                # Preparation alone is retryable; no downstream business call has begun.

    @staticmethod
    def validate_metadata(values, partial=False):
        if ((not partial or "name" in values)
                and (not isinstance(values.get("name"), str)
                     or not 1 <= len(values["name"].strip()) <= 128)):
            raise ValueError("Name must contain 1–128 characters")
        if "tags" in values and (not isinstance(values["tags"], list)
                                or any(not isinstance(tag, str) for tag in values["tags"])):
            raise ValueError("Tags must be an array of strings")
        if "description" in values and not isinstance(values["description"], str):
            raise ValueError("Description must be a string")

    async def create_in_session(self, session, values, *, user_id=None):
        """Create local rows only; caller owns the transaction and post-commit discovery."""
        values = dict(values)
        self.validate_metadata(values)
        transport = values.get("transport", "stdio")
        transport = "streamable-http" if transport == "http" else transport
        config = values.get("config", {})
        mode = values.get("mode", "lazy")
        isolation = values.get("isolation", "service")
        if mode not in {"eager", "lazy", "disabled"}:
            raise ValueError("Invalid mode")
        validate_service_config(transport, isolation, config)
        name = values["name"].strip()
        requested_slug = values.get("slug")
        if requested_slug:
            validate_slug(requested_slug)
            slug = requested_slug
        else:
            base, suffix = slug_base(name), 2
            slug = base
            while await session.scalar(select(McpServer.id).where(McpServer.slug == slug)):
                tail = "-" + str(suffix)
                slug = base[:64 - len(tail)].rstrip("-") + tail
                suffix += 1
        row = McpServer(
            id=str(uuid4()), name=name, slug=slug,
            description=values.get("description", ""), tags=values.get("tags", []),
            transport=transport, mode=mode, isolation=isolation, config=self.seal({}),
            auth_type=config.get("auth", {}).get("type", "none"),
            config_isolation=config.get("auth", {}).get("config_isolation", "shared")
                if config.get("auth", {}).get("type") == "oauth" else None,
        )
        session.add(row)
        await session.flush()
        stored = await self.credentials.persist_config(session, row, config, isolation, user_id)
        row.config = self.seal(stored)
        await session.flush()
        return row

    async def create(self, values, *, authorize=None, user_id=None, discover=True):
        try:
            async with self.db.locked() as session:
                if authorize:
                    await authorize(session)
                row = await self.create_in_session(session, values, user_id=user_id)
        except IntegrityError:
            raise HTTPException(409, "MCP slug already exists") from None
        await self.logs.audit("mcp.create", user_id, {"mcp_id": row.id},
                              target_type="mcp", target_id=row.id, target_name=row.name)
        self.runtime.versions[row.id] = row.revision
        if discover:
            await self.refresh_after_change(row, user_id, authorize)
        return await self.get(row.id)

    async def update(self, server_id, values, *, authorize=None, user_id=None):
        if self.oauth:
            await self.oauth.sync_public_url(user_id)
        async with self.locks.setdefault(server_id, asyncio.Lock()):
            old = await self.get(server_id)
            if values.get("slug", old.slug) != old.slug:
                raise ValueError("Service slug is immutable; create a copy to use a different slug")
            self.validate_metadata(values, partial=True)
            if "revision" in values and values["revision"] != old.revision:
                raise HTTPException(409, "Configuration changed; reload before saving")
            previous = await self.credentials.materialize(
                old, user_id, require=False
            )
            config = restore(values.get("config", previous), previous)
            transport = values.get("transport", old.transport)
            transport = "streamable-http" if transport == "http" else transport
            mode = values.get("mode", old.mode)
            isolation = values.get("isolation", old.isolation)
            if mode not in {"eager", "lazy", "disabled"}:
                raise ValueError("Invalid mode")
            validate_service_config(transport, isolation, config)
            from .oauth_config import comparable_settings

            before_auth, after_auth = previous.get("auth", {}), config.get("auth", {})
            oauth_changed = (before_auth.get("type") == "oauth" or after_auth.get("type") == "oauth") and (
                comparable_settings(before_auth) != comparable_settings(after_auth)
                or previous.get("url") != config.get("url")
                or old.isolation != isolation or old.transport != transport
                or before_auth.get("config_isolation", "shared") != after_auth.get("config_isolation", "shared"))
            if authorize:
                await authorize()
            async with self.changing(server_id):
                async with self.db.locked() as s:
                    if authorize:
                        await authorize(s)
                    row = await s.get(McpServer, server_id)
                    stored = await self.credentials.persist_config(
                        s, row, config, isolation, user_id
                    )
                    for key in ("name", "description", "tags"):
                        if key in values:
                            setattr(row, key, values[key])
                    row.mode = mode
                    row.isolation = isolation
                    row.transport = transport
                    row.auth_type = config.get("auth", {}).get("type", "none")
                    row.config_isolation = config.get("auth", {}).get("config_isolation", "shared") \
                        if row.auth_type == "oauth" else None
                    row.config = self.seal(stored)
                    row.revision += 1
                    await s.flush()
                    if oauth_changed and self.oauth:
                        cleared_states = self.oauth.flows.invalidate(server_id)
                await self.logs.audit("mcp.update", user_id,
                    {"mcp_id": row.id, "revision": row.revision, "changed_fields": sorted(values)},
                    target_type="mcp", target_id=row.id, target_name=row.name)
                if oauth_changed and self.oauth:
                    await self.oauth.audit("oauth.state_invalidated", user_id, server_id,
                                           reason="config_changed", cleared_states=cleared_states)
                # A revision or ownership change must never publish stale tools.
                self.runtime.versions[server_id] = row.revision
                await asyncio.to_thread(self.cache_store.delete_prefix, server_id + "-")
                for key in list(self._cache_failures):
                    if key.startswith(server_id + "-"):
                        self._cache_failures.pop(key, None)
                for key in list(self.runtime.credential_versions):
                    if key[0] == server_id:
                        self.runtime.credential_versions.pop(key, None)
                await self.refresh_after_change(row, user_id, authorize)
                return await self.get(row.id)

    async def delete(self, server_id, *, authorize=None):
        async with self.locks.setdefault(server_id, asyncio.Lock()):
            if authorize:
                await authorize()
            async with self.changing(server_id):
                async with self.db.locked() as s:
                    if authorize:
                        await authorize(s)
                    row = await s.get(McpServer, server_id)
                    if not row:
                        raise HTTPException(404, "MCP not found")
                    await s.delete(row)
                    await self.credentials.delete_server(s, server_id)
                    await s.execute(delete(SystemSetting).where(SystemSetting.key == "tests:" + server_id))
                    for model in (User, ApiToken):
                        for principal in (await s.scalars(select(model))).all():
                            if server_id in principal.mcp_ids:
                                principal.mcp_ids = [item for item in principal.mcp_ids if item != server_id]
                    anonymous = await s.get(SystemSetting, "anonymous_mcp_ids")
                    if anonymous:
                        anonymous.value = [item for item in anonymous.value if item != server_id]
                from .audit_events import AUDIT_CONTEXT

                await self.logs.audit("mcp.delete", (AUDIT_CONTEXT.get() or {}).get("user_id"),
                    {"mcp_id": server_id}, target_type="mcp", target_id=server_id, target_name=row.name)
                invalidate_setting_cache(
                    self.db, ["tests:" + server_id, "anonymous_mcp_ids"]
                )
                try:
                    await asyncio.to_thread(self.cache_store.delete_prefix, server_id + "-")
                except OSError:
                    if not self.on_cache_change:
                        raise
                    # The row is deleted; the callback retries physical cleanup in the background.
                for key in list(self._cache_failures):
                    if key.startswith(server_id + "-"):
                        self._cache_failures.pop(key, None)
                await self.index.detach_server(server_id)
                if self.on_cache_change:
                    await self.on_cache_change(server_id)

    async def refresh_targets(self, server_id=None):
        targets = []
        for row in await self.rows(ids=[server_id] if server_id else None):
            if row.mode == "disabled":
                continue
            if row.isolation != "user":
                targets.append({"server_id": row.id, "user_id": None})
                continue
            owners = await self.credentials.owners(row.id)
            prefix = "oauth:" + row.id + ":"
            async with self.db.session() as session:
                legacy = await session.scalars(select(SystemSetting).where(
                    SystemSetting.key.startswith(prefix, autoescape=True)
                ))
                owners.update(
                    item.key[len(prefix):] for item in legacy if item.value
                )
                for owner in sorted(owners - {"service", "anonymous"}):
                    user = await session.get(User, owner)
                    if (
                        user
                        and not user.disabled
                        and (
                            user.role == "admin"
                            or user.scope_mode == "all"
                            or row.id in user.mcp_ids
                        )
                    ):
                        targets.append({"server_id": row.id, "user_id": owner})
        return targets

    async def authorize_target(self, target):
        if target["user_id"]:
            async with self.db.session() as session:
                user = await session.get(User, target["user_id"])
                if (not user or user.disabled or (user.role != "admin" and user.scope_mode != "all"
                                                 and target["server_id"] not in user.mcp_ids)):
                    raise GatewayError("permission_revoked", "Scheduled refresh owner lost access")

    async def refresh_target(self, target):
        return await self.refresh_index(target["server_id"], target["user_id"],
                                  authorize=lambda: self.authorize_target(target))

    async def warm_target(self, target):
        return await self.warm(target["server_id"], target["user_id"],
                               authorize=lambda: self.authorize_target(target))

    async def retry_disabled(self, server_id, user_id, *, authorize, allow_manual=False):
        """Only an explicit administrator operation may retry a disabled policy."""
        await authorize()
        row = await self.get(server_id)
        if row.mode != "disabled":
            return False
        cache = self.cached(row, user_id)
        if not allow_manual and not cache.get("auto_disabled"):
            raise GatewayError("disabled", "MCP was manually disabled; use Start or select lazy/eager to enable it")
        mode = cache.get("recovery_mode", "lazy")
        if mode not in {"lazy", "eager"}:
            mode = "lazy"
        row = await self.update(server_id, {"mode": mode, "revision": row.revision},
                                authorize=authorize, user_id=user_id)
        cache = self.cached(row, user_id)
        if row.mode == "disabled" or cache["cache_status"] != "ready":
            raise GatewayError(cache.get("cache_error_code") or "discovery_failed",
                               cache.get("cache_error") or "MCP directory verification failed")
        return True

    async def ensure_ready(self, row, user_id=None, *, authorize=None):
        """Refresh missing/failed contracts only for an explicit authorized operation."""
        async def ready(current):
            scope = await self.index_scope(current, user_id)
            summary = (await self.index.get_summaries([scope])).get(current.id)
            return summary["cache_status"] == "ready" if summary else self.cached(current, user_id)["cache_status"] == "ready"

        if row.mode == "disabled":
            await self.spec(row, user_id)
        if await ready(row):
            return self.cached(row, user_id)
        key = (row.id, row.revision, catalog_owner(row.isolation, user_id))
        async with self.discovery_locks.setdefault(key, asyncio.Lock()):
            row = await self.get(row.id)
            if not await ready(row):
                await self.refresh_index(row.id, user_id, authorize=authorize)
            return self.cached(row, user_id)

    async def _index_scope(self, session, row, owner):
        if row.isolation != "user" and owner != "service":
            raise GatewayError("credential_changed", "Catalog owner does not match service isolation")
        keys = self._index_credential_keys(row, owner)
        stored = dict((await session.execute(select(
            SystemSetting.key, SystemSetting.value
        ).where(SystemSetting.key.in_(keys)))).all())
        return self._scope_from_settings(row, owner, stored)

    def _index_credential_keys(self, row, owner):
        return {
            self.credentials.key(row.id, owner), self.credentials.key(row.id, "service"),
            "oauth:" + row.id + ":" + owner,
        }

    def _scope_from_settings(self, row, owner, stored):
        values = [[key, stored.get(key)] for key in sorted(self._index_credential_keys(row, owner))]
        # Hash sealed payloads, never place credentials in the public catalog DTO.
        generation = hashlib.sha256(strict_json_dumps([row.revision, values]).encode()).hexdigest()
        return CatalogScope(row.id, owner, row.revision, generation)

    async def directory_version(self):
        from sqlalchemy import text

        from .catalog_control import CatalogDirectoryClock

        async def primary():
            async with self.db.session() as session:
                return await session.scalar(select(CatalogDirectoryClock.version).where(CatalogDirectoryClock.id == 1))

        async def derived():
            async with self.db.derived.session() as session:
                return await session.scalar(text("SELECT value FROM derived_identity WHERE key='repository'"))

        version, identity = await asyncio.gather(primary(), derived())
        if version is None or identity is None:
            raise GatewayError("catalog_unavailable", "Directory version fence is unavailable")
        return version, identity

    async def index_scopes(self, rows, user_id=None):
        """Batch primary authority and credentials; no oversized SQLite IN lists."""
        from sqlalchemy.orm import load_only

        from .catalog_control import CatalogControl

        result = {}
        for offset in range(0, len(rows), 250):
            batch = rows[offset:offset + 250]
            async with self.db.session() as session:
                current = list((await session.scalars(select(McpServer).options(load_only(
                    McpServer.id, McpServer.revision, McpServer.isolation
                )).where(McpServer.id.in_([row.id for row in batch])))).all())
                expected = {row.id: (row.revision, row.isolation) for row in batch}
                if {row.id: (row.revision, row.isolation) for row in current} != expected:
                    raise GatewayError("revision_changed", "MCP configuration changed")
                owners = {row.id: catalog_owner(row.isolation, user_id) for row in current}
                keys = sorted({key for row in current for key in self._index_credential_keys(row, owners[row.id])})
                stored = {}
                for part in range(0, len(keys), 250):
                    stored.update((await session.execute(select(SystemSetting.key, SystemSetting.value).where(
                        SystemSetting.key.in_(keys[part:part + 250])))).all())
                scopes = {row.id: self._scope_from_settings(row, owners[row.id], stored) for row in current}
                controls = {item.scope_key: item for item in (await session.scalars(select(CatalogControl).options(load_only(
                    CatalogControl.scope_key, CatalogControl.revision, CatalogControl.credential_generation,
                    CatalogControl.health)).where(
                    CatalogControl.scope_key.in_([scope_key(scope) for scope in scopes.values()])))).all()}
                for scope in scopes.values():
                    control = controls.get(scope_key(scope))
                    health = control.health if control and (control.revision, control.credential_generation) == (
                        scope.revision, scope.credential_generation) else {}
                    self._remember_health(scope, health)
                result.update(scopes)
        return result

    async def index_scope(self, row, user_id=None):
        from sqlalchemy.orm import load_only

        from .catalog_control import CatalogControl
        async with self.db.session() as session:
            current = await session.get(McpServer, row.id, populate_existing=True, options=[load_only(
                McpServer.id, McpServer.revision, McpServer.isolation)])
            if not current or current.revision != row.revision:
                raise GatewayError("revision_changed", "MCP configuration changed")
            scope = await self._index_scope(session, current, catalog_owner(current.isolation, user_id))
            control = await session.get(CatalogControl, scope_key(scope), options=[load_only(
                CatalogControl.scope_key, CatalogControl.revision, CatalogControl.credential_generation,
                CatalogControl.health)])
            health = control.health if control and (control.revision, control.credential_generation) == (
                scope.revision, scope.credential_generation) else {}
            self._remember_health(scope, health)
            return scope

    @tracked("refresh")
    async def refresh_index(self, server_id, user_id=None, *, authorize=None):
        if authorize:
            await authorize()
        row = await self.get(server_id)
        had_ready = self.cached(row, user_id)["cache_status"] == "ready"
        for attempt in range(2):
            lease, spec, scope, expected_refresh = None, None, None, None
            try:
                spec, scope = await self.prepare_scope(row, user_id, authorize=authorize, retry=False)
                expected_refresh = await self.index.refresh_version(scope)
                lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
                result = await self.runtime.perform(
                    spec, lease.id, "ingest_catalog", self.index, scope, authorize,
                    authorize=authorize, business=False,
                )
                if authorize:
                    await authorize()
                current = await self.get(server_id)
                if await self.index_scope(current, user_id) != scope:
                    raise GatewayError("credential_changed", "MCP scope changed while publishing capabilities")
                cached = self.cached(current, user_id)
                cached.update(
                    **{key: [] for key in CAPABILITY_KEYS},
                    **{key: result[key] for key in ("tool_count", "resource_count", "prompt_count", "template_count")},
                    cache_status="ready", cache_at=now().isoformat(), cache_attempt_at=now().isoformat(),
                    cache_error=None, cache_error_code=None, runtime_status="stopped",
                    startup_failure_count=0, failed_gateway_names=[], last_startup_error_code=None, last_startup_error=None,
                    last_startup_failure_at=None, last_refresh_error_code=None,
                    last_refresh_error=None, last_refresh_failure_at=None,
                )
                expected_refresh = await self.index.refresh_version(scope)
                self._remember_health(scope, {})
                await run_blocking(self.save_cache, current, cached, user_id)
                return result
            except Exception as exc:  # noqa: BLE001 - normalize third-party transport errors
                config = spec.config if spec else self.unseal(row.config)
                exc = safe_gateway_error(exc, config=config)
                if (exc.code == "credential_changed" and attempt == 0
                        and config.get("auth", {}).get("type") == "oauth"):
                    # OAuth discovery is read-only; discard the stale snapshot and prepare anew.
                    continue
                failure = await self.record_refresh_failure(
                    row, exc, user_id, had_ready=had_ready, scope=scope, expected_refresh=expected_refresh,
                )
                raise failure
            finally:
                if lease:
                    await self.runtime.release(lease.id)

    @tracked("refresh")
    async def refresh(self, server_id, user_id=None, *, authorize=None):
        if authorize:
            await authorize()
        row = await self.get(server_id)
        lease, spec, scope, expected_refresh = None, None, None, None
        had_ready = self.cached(row, user_id)["cache_status"] == "ready"
        try:
            spec, scope = await self.prepare_scope(row, user_id, authorize=authorize)
            expected_refresh = await self.index.refresh_version(scope)
            if authorize:
                await authorize()
            lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
            result = await self.runtime.perform(spec, lease.id, "discover", authorize=authorize, business=False)
            return await self.store_discovery(row, result, user_id, spec=spec)
        except Exception as exc:  # noqa: BLE001 - normalize third-party transport errors
            exc = safe_gateway_error(exc, config=spec.config if spec else self.unseal(row.config))
            failure = await self.record_refresh_failure(
                row, exc, user_id, had_ready=had_ready, scope=scope, expected_refresh=expected_refresh,
            )
            raise failure
        finally:
            if lease:
                await self.runtime.release(lease.id)

    @tracked("refresh")
    async def warm(self, server_id, user_id=None, *, explicit=False, authorize=None, expected_revision=None):
        row, lease, spec, success = None, None, None, False
        scope, expected_refresh = None, None
        had_ready = False
        try:
            if authorize:
                await authorize()
            row = await self.get(server_id)
            if expected_revision is not None and row.revision != expected_revision:
                return
            had_ready = self.cached(row, user_id)["cache_status"] == "ready"
            spec, scope = await self.prepare_scope(row, user_id, authorize=authorize)
            expected_refresh = await self.index.refresh_version(scope)
            lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
            result = await self.runtime.perform(spec, lease.id, "discover", authorize=authorize, business=False)
            await self.store_discovery(row, result, user_id, spec=spec)
            success = True
            return result
        except Exception as exc:
            exc = safe_gateway_error(exc, config=spec.config if spec else self.unseal(row.config) if row else {})
            if row:
                exc = await self.record_refresh_failure(
                    row, exc, user_id, had_ready=had_ready, scope=scope, expected_refresh=expected_refresh,
                )
            await self.logs.audit("mcp.start.failed", user_id, {"server_id": server_id, "error_type": type(exc).__name__})
            raise
        finally:
            if lease:
                await self.runtime.release(lease.id, keep_alive=success and explicit)

    @asynccontextmanager
    async def changing(self, server_id):
        await self.stop_for_change(server_id)
        yield

    async def stop_for_change(self, server_id):
        try:
            await self.runtime.stop_server(server_id, require_idle=True, reason="configuration_changed")
        except GatewayError as exc:
            if exc.code == "busy":
                raise HTTPException(409, "MCP has active calls; retry after completion") from exc
            raise

    async def tool_definition(self, row, name, user_id=None):
        scope = await self.index_scope(row, user_id)
        definition = await self.index.get_definition(scope, kind="tools", name=name)
        if definition is not None or await self.index.has_scope(scope):
            return definition
        # Temporary compatibility for pre-cutover caches; only this service is inspected.
        tool = next((value for value in self.cached_view(row, user_id)["tools"] if value["name"] == name), None)
        return await asyncio.to_thread(copy.deepcopy, tool) if tool is not None else None

    async def validate_tool_arguments(self, tool, arguments):
        schema = tool.get("inputSchema", {"type": "object"})
        def identity_and_cost():
            import sys

            encoded = strict_json_dumps(schema).encode()
            seen, pending, retained = set(), [schema], 8192
            while pending:
                value = pending.pop()
                if id(value) in seen:
                    continue
                seen.add(id(value))
                retained += sys.getsizeof(value)
                if isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
            return hashlib.sha256(encoded).hexdigest(), retained
        key, cost = await asyncio.to_thread(identity_and_cost)
        cached = self._validators.get(key)
        if cached is None:
            validator = await asyncio.to_thread(Draft202012Validator, schema, registry=LOCAL_SCHEMA_REGISTRY)
            while self._validators and (self._validator_bytes + cost > 32 * 1024 * 1024
                                        or len(self._validators) >= 4096):
                _old_key, (_old_validator, old_cost) = self._validators.popitem(last=False)
                self._validator_bytes -= old_cost
            if cost <= 32 * 1024 * 1024:
                existing = self._validators.get(key)
                if existing is None:
                    self._validators[key] = (validator, cost)
                    self._validator_bytes += cost
                else:
                    validator = existing[0]
        else:
            self._validators.move_to_end(key)
            validator = cached[0]
        await asyncio.to_thread(validate_local, validator, arguments)

    @tracked("call")
    async def call(self, row, lease, name, arguments, *, user=None, token=None, authorize=None, source="gateway",
                   response_processor=None):
        if authorize:
            await authorize()
        _prepared, prepared_scope = await self.prepare_scope(row, user.id if user else None, authorize=authorize)
        # A definition read already validates the current ready generation,
        # primary revision, credential fingerprint and control epoch. Avoid a
        # second summary read on the healthy path; missing contracts still use
        # the normal single-flight discovery/recovery path.
        tool = await self.index.get_definition(prepared_scope, kind="tools", name=name)
        if tool is None:
            await self.ensure_ready(row, user.id if user else None, authorize=authorize)
            tool = await self.tool_definition(row, name, user.id if user else None)
        event = {"user_id": user.id if user else None,
                 "username": user.username if user else "anonymous", "token_id": token.id if token else None,
                 "token_name": token.name if token else "", "mcp_id": row.id, "mcp_name": row.name,
                 "tool_name": name, "arguments": arguments, "source": source, "status": "running",
                 "lease_id": lease.id, "transport": "stdio-bridge" if lease.kind == "bridge" else "http"}
        started = time.monotonic()
        event["id"] = await self.logs.append(event)
        try:
            if tool is None:
                raise GatewayError("tool_not_found", "Tool is absent from the current MCP directory")
            await self.validate_tool_arguments(tool, arguments)
            if authorize:
                await authorize()
            spec, _scope = await self.prepare_scope(row, user.id if user else None, authorize=authorize)
            from .tool_headers import tool_context, validate_header_schema

            context = {"name": name, "tool": tool}
            async def before_dispatch():
                if authorize:
                    await authorize()
                # Startup may have replaced stale schemas before this queued call.
                current_tool = await self.tool_definition(row, name, user.id if user else None)
                if current_tool is None:
                    raise GatewayError("tool_not_found", "Tool is absent from the current MCP directory")
                await self.validate_tool_arguments(current_tool, arguments)
                validate_header_schema(current_tool.get("inputSchema", {}))
                context["tool"] = current_tool
                if authorize:
                    await authorize()
            binding = tool_context.set(context)
            try:
                result = await self.runtime.call(spec, lease.id, name, arguments, authorize=before_dispatch)
            finally:
                tool_context.reset(binding)
            event.update(status="tool_error" if result.get("isError") else "success", result=result)
            if result.get("isError"):
                outcome = current_operation.get()
                if outcome is not None:
                    reason = next((block.get("text", "") for block in result.get("content", [])
                                   if block.get("type") == "text"), "Downstream tool reported an error")
                    outcome.error = GatewayError("tool_error", str(reason)[:500])
            response = result
        except asyncio.CancelledError:
            event.update(status="cancelled", error="Caller cancelled; dispatched side effects may have occurred")
            raise
        except Exception as exc:
            event.update(status="outcome_unknown" if getattr(exc, "code", "") == "outcome_unknown"
                         else "gateway_error", error=str(exc)[:2000])
            raise
        finally:
            event["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            try:
                # Keep the downstream outcome/result even if later response processing fails.
                if response_processor is not None and "result" in event:
                    response, added_content = await response_processor(result)
                    if added_content:
                        event["gateway_processing"] = {"added_content": added_content}
            finally:
                try:
                    await asyncio.shield(self.logs.append(event))
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("Final call log write failed; preserving downstream outcome")
        return response
