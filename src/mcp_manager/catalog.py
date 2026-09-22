"""Catalog, sealed configuration and revision-scoped capability caches."""
import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from weakref import WeakValueDictionary

from cryptography.fernet import Fernet
from fastapi import HTTPException
from jsonschema import Draft202012Validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .credentials import catalog_owner, credential_owner
from .database import ApiToken, McpServer, SystemSetting, User, invalidate_setting_cache, now
from .identity import public as public_model
from .identity import revalidate_user
from .jsonl_store import JsonlStore
from .runtime import GatewayError, ServerSpec
from .transports import validate_service_config

SECRET_KEYS = {
    "password", "current_password", "secret", "secret_key", "database_url",
    "token", "access_token", "refresh_token", "client_secret",
    "authorization", "cookie", "api_key", "value",
}
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


def diagnostic_cause(exc):
    """Return the most specific downstream cause from wrapped failures."""
    current = exc
    while getattr(current, "__cause__", None) is not None:
        current = current.__cause__
    if isinstance(current, BaseExceptionGroup):
        leaves = []

        def collect(group):
            for child in group.exceptions:
                if isinstance(child, BaseExceptionGroup):
                    collect(child)
                else:
                    leaves.append(diagnostic_cause(child))

        collect(current)
        return next(
            (
                item
                for item in leaves
                if getattr(getattr(item, "response", None), "status_code", None)
            ),
            leaves[-1] if leaves else current,
        )
    return current


def sanitize_diagnostic_error(exc, secrets):
    """Expose useful downstream type/status while redacting sensitive values."""
    cause = diagnostic_cause(exc)
    message = str(cause) or type(cause).__name__
    for secret in sorted(
        {str(value) for value in secrets if value}, key=len, reverse=True
    ):
        message = message.replace(secret, "[REDACTED]")
    message = re.sub(
        r"(?i)\b(?:authorization|cookie|set-cookie)\b[^;\r\n]*",
        "[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)([?&](?:code|access_token|refresh_token)=)[^&\s]+",
        r"\1[REDACTED]",
        message,
    )
    details = {
        "error_type": type(cause).__name__,
        "downstream_status": getattr(
            getattr(cause, "response", None), "status_code", None
        ),
    }
    return GatewayError("diagnosis_failed", message[:500], details)


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
        self.cache_store = JsonlStore(self.cache_dir / "catalog.jsonl", fail_closed=True)
        self._cache_failures = {}
        self.startup_failure_threshold = 3
        def legacy_cache(path, value):
            server_id, revision, owner = path.stem.rsplit("-", 2)
            if not isinstance(value, dict):
                raise ValueError("Invalid legacy cache")
            return server_id + "-" + owner, {"revision": int(revision), "data": value}
        self.cache_store.migrate_json(legacy_cache)

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

    def seal(self, value):
        return {"sealed": self.fernet.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()}

    def unseal(self, value):
        return json.loads(self.fernet.decrypt(value["sealed"])) if "sealed" in value else copy.deepcopy(value)

    async def get(self, server_id):
        async with self.db.session() as s:
            row = await s.get(McpServer, server_id)
        if not row:
            raise HTTPException(404, "MCP not found")
        return row

    async def rows(self, ids=None):
        statement = select(McpServer).order_by(McpServer.created_at.desc())
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
            tool_count=len(cache["tools"]),
            cache_at=cache.get("cache_at"),
            cache_error=cache.get("cache_error"),
            cache_status=cache.get("cache_status"),
            cache_error_code=cache.get("cache_error_code"),
            cache_attempt_at=cache.get("cache_attempt_at"),
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
        runtime_user_id = user_id if self.failure_scope(row) == "user" else None
        instances = [
            item for item in self.runtime.status(user_id=runtime_user_id)
            if item["server_id"] == row.id
        ]
        if any(item["in_flight"] > 0 for item in instances):
            return "running"
        if any(item["phase"] == "ready" for item in instances):
            return "ready"
        return "failed" if self.cached(row, user_id)["runtime_status"] == "failed" else "stopped"

    def failure_details(self, row, user_id=None):
        cache = self.cached(row, user_id)
        return {
            "mcp_id": row.id,
            "mcp_name": row.name,
            "status": self.service_status(row, user_id),
            "startup_failure_count": cache["startup_failure_count"],
            "failure_reason": cache["last_startup_error"],
            "failure_scope": self.failure_scope(row),
            "recovery": "Ask an administrator to repair or refresh this MCP service.",
        }

    def failed_error(self, row, user_id=None):
        if self.service_status(row, user_id) != "failed":
            return None
        details = self.failure_details(row, user_id)
        return GatewayError(
            "mcp_failed",
            details["failure_reason"] or "MCP service failed to start",
            details,
        )

    def cached(self, row, user_id=None):
        def current(key):
            saved = self._cache_failures.get(key) or self.cache_store.get(key)
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
        value = empty | copy.deepcopy(saved or {})
        value["failure_scope"] = self.failure_scope(row)
        if value["cache_status"] == "empty" and value["cache_at"]:
            value["cache_status"] = "error" if value["cache_error"] else "ready"
        if row.mode == "disabled" or value["runtime_status"] == "failed" or value["cache_status"] != "ready":
            value = value | {key: [] for key in CAPABILITY_KEYS}
            if row.mode == "disabled" and value["cache_status"] != "error":
                value["cache_status"] = "disabled"
        return value

    def delete_cache(self, row, user_id=None):
        """Delete one canonical owner cache without crossing isolation scopes."""
        key = self.cache_key(row, user_id)
        self.cache_store.set(key, None)
        self._cache_failures.pop(key, None)

    def save_cache(self, row, value, user_id=None):
        key = self.cache_key(row, user_id)
        try:
            self.cache_store.set(key, {"revision": row.revision, "data": value})
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

    def cache_failure(self, row, exc, user_id=None):
        if (
            self.runtime.versions.get(row.id, row.revision) != row.revision
            or getattr(exc, "code", "") in {"credential_changed", "revision_changed"}
        ):
            return
        code = getattr(exc, "code", "discovery_failed")
        value = self.cached(row, user_id)
        value.update({key: [] for key in CAPABILITY_KEYS})
        value.update(
            cache_at=None,
            cache_status="auth_required" if code == "auth_required" else "error",
            cache_error_code=code,
            cache_error=str(exc)[:500],
            cache_attempt_at=now().isoformat(),
            failure_scope=self.failure_scope(row),
        )
        try:
            self.save_cache(row, value, user_id)
        except GatewayError as write_error:
            if write_error.code != "cache_write_failed":
                raise
            logging.getLogger(__name__).warning("%s", write_error)

    async def record_start_success(self, row, user_id=None):
        current = await self.get(row.id)
        if current.revision != row.revision or current.mode == "disabled":
            raise GatewayError("revision_changed", "MCP configuration changed during startup")
        value = self.cached(current, user_id)
        if (
            value["runtime_status"] == "stopped"
            and value["startup_failure_count"] == 0
            and value["last_startup_error"] is None
            and not value["failed_gateway_names"]
        ):
            return value
        value.update(
            runtime_status="stopped",
            startup_failure_count=0,
            last_startup_error_code=None,
            last_startup_error=None,
            last_startup_failure_at=None,
            failure_scope=self.failure_scope(current),
            failed_gateway_names=[],
        )
        self.save_cache(current, value, user_id)
        return value

    async def record_start_failure(self, row, exc, user_id=None):
        code = getattr(exc, "code", "startup_failed")
        if code in {
            "credential_changed",
            "revision_changed",
            "permission_revoked",
            "forbidden",
            "lease_expired",
            "queue_timeout",
            "stopped",
            "disabled",
            "shutting_down",
            "tool_not_found",
            "downstream_error",
            "outcome_unknown",
        }:
            return exc
        async with self.db.locked() as session:
            current = await session.get(McpServer, row.id)
            if not current or current.revision != row.revision or current.mode == "disabled":
                return exc
            value = self.cached(current, user_id)
            count = int(value.get("startup_failure_count", 0)) + 1
            failed_at = now().isoformat()
            value.update(
                runtime_status="failed" if count >= self.startup_failure_threshold else "stopped",
                startup_failure_count=count,
                last_startup_error_code=code,
                last_startup_error=str(exc)[:500],
                last_startup_failure_at=failed_at,
                failure_scope=self.failure_scope(current),
            )
            if value["cache_status"] != "ready":
                value.update(
                    cache_status="error",
                    cache_at=None,
                    cache_error_code=code,
                    cache_error=str(exc)[:500],
                    cache_attempt_at=failed_at,
                )
            if count >= self.startup_failure_threshold:
                names = set(value.get("failed_gateway_names", []))
                names.update(alias(current.slug, tool["name"]) for tool in value["tools"])
                names.update(current.id + "__" + tool["name"] for tool in value["tools"])
                value.update(
                    failed_gateway_names=sorted(names),
                    cache_status="error",
                    cache_at=None,
                    **{key: [] for key in CAPABILITY_KEYS},
                )
            self.save_cache(current, value, user_id)
        details = self.failure_details(current, user_id)
        if not isinstance(exc, GatewayError):
            exc = GatewayError(code, str(exc) or "MCP startup failed")
        exc.details.update(details)
        exc.startup_failure_recorded = True
        return exc

    async def record_refresh_failure(self, row, exc, user_id=None, *, had_ready):
        code = getattr(exc, "code", "discovery_failed")
        if (
            self.runtime.versions.get(row.id, row.revision) != row.revision
            or code in {"credential_changed", "revision_changed", "permission_revoked", "forbidden",
                        "lease_expired", "queue_timeout", "stopped", "disabled", "shutting_down"}
        ):
            return exc
        if getattr(exc, "startup_failure_recorded", False):
            return exc
        if had_ready:
            value = self.cached(row, user_id)
            value.update(
                last_refresh_error_code=code,
                last_refresh_error=str(exc)[:500],
                last_refresh_failure_at=now().isoformat(),
            )
            self.save_cache(row, value, user_id)
            return exc
        if code == "auth_required" or isinstance(exc, HTTPException):
            self.cache_failure(row, exc, user_id)
            return exc
        failure = GatewayError("startup_failed", str(exc) or "Initial tool discovery failed")
        return await self.record_start_failure(row, failure, user_id)

    def mark_refreshing(self, row, user_id=None):
        if row.mode != "disabled" and self.runtime.versions.get(row.id, row.revision) == row.revision:
            value = self.cached(row, user_id)
            value.update(cache_status="refreshing", cache_attempt_at=now().isoformat())
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
        if row.revision == spec.revision:
            user_id = spec.credential_owner if spec.credential_owner not in {"", "service"} else None
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
        self.save_cache(current, result, user_id)
        return result

    async def connection_ready(self, spec, connection):
        if spec.id.startswith("diagnose-"):
            return
        row = await self.get(spec.id)
        if row.revision != spec.revision:
            raise GatewayError("revision_changed", "MCP configuration changed during startup")
        user_id = spec.credential_owner if spec.credential_owner not in {"", "service"} else None
        await self.record_start_success(row, user_id)

    async def refresh_after_change(self, row, user_id=None, authorize=None):
        if row.mode == "disabled":
            self.save_cache(row, {"tools": [], "resources": [], "prompts": [], "templates": [],
                                  "cache_status": "disabled"}, user_id)
            return
        try:
            if row.isolation == "session":
                await self.refresh(row.id, user_id, authorize=authorize)
            else:
                await self.warm(row.id, user_id, authorize=authorize, expected_revision=row.revision)
        except Exception:
            # Saving valid configuration succeeds even when a remote service is
            # offline; the attempt and actionable status are recorded in its cache.
            pass

    async def spec(self, row, user_id=None, *, allow_failed=False):
        if row.mode == "disabled":
            raise GatewayError("disabled", "MCP is disabled; repair configuration or authorization, then enable it")
        if not allow_failed:
            failure = self.failed_error(row, user_id)
            if failure is not None:
                raise failure
        if self.credentials is None:
            config = self.unseal(row.config)
        else:
            config = await self.credentials.materialize(row, user_id)
        auth = config.get("auth", {})
        auth_type = auth.get("type", "none")
        if auth_type == "oauth" and self.oauth is not None:
            await self.oauth.credentials(row, user_id)
            config = await self.credentials.materialize(row, user_id)
            auth = config.get("auth", {})
        if auth_type == "oauth" and not auth.get("access_token"):
            raise GatewayError(
                "auth_required", "Complete OAuth authorization in the Web console"
            )
        owner = (
            credential_owner(row.isolation, user_id)
            if auth_type in {"bearer", "oauth"}
            else ""
        )
        scope = (
            hashlib.sha256(
                json.dumps(auth, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]
            if owner
            else ""
        )
        if owner:
            self.runtime.credential_versions[(row.id, owner)] = scope
        return ServerSpec(
            row.id,
            row.transport,
            config,
            row.mode,
            row.isolation,
            row.revision,
            scope,
            owner,
        )

    @staticmethod
    def validate_metadata(values, partial=False):
        if not partial or "name" in values:
            if not isinstance(values.get("name"), str) or not 1 <= len(values["name"].strip()) <= 128:
                raise ValueError("Name must contain 1–128 characters")
        if "tags" in values and (not isinstance(values["tags"], list)
                                or any(not isinstance(tag, str) for tag in values["tags"])):
            raise ValueError("Tags must be an array of strings")
        if "description" in values and not isinstance(values["description"], str):
            raise ValueError("Description must be a string")

    async def create(self, values, *, authorize=None, user_id=None):
        values = dict(values)
        self.validate_metadata(values)
        transport = values.get("transport", "stdio")
        if transport == "http":
            transport = "streamable-http"
        config = values.get("config", {})
        mode = values.get("mode", "lazy")
        isolation = values.get("isolation", "service")
        if mode not in {"eager", "lazy", "disabled"}:
            raise ValueError("Invalid mode")
        validate_service_config(transport, isolation, config)
        name = values.get("name", "").strip()
        if not name or len(name) > 128:
            raise ValueError("Name must contain 1–128 characters")
        requested_slug = values.get("slug")
        if requested_slug:
            validate_slug(requested_slug)
        try:
            async with self.db.locked() as s:
                if authorize:
                    await authorize(s)
                if requested_slug:
                    slug = requested_slug
                else:
                    base = slug_base(name)
                    existing = set((await s.scalars(select(McpServer.slug))).all())
                    slug = base
                    suffix = 2
                    while slug in existing:
                        tail = "-" + str(suffix)
                        slug = base[:64 - len(tail)].rstrip("-") + tail
                        suffix += 1
                row = McpServer(
                    id=str(uuid4()),
                    name=name,
                    slug=slug,
                    description=values.get("description", ""),
                    tags=values.get("tags", []),
                    transport=transport,
                    mode=mode,
                    isolation=isolation,
                    config=self.seal({}),
                )
                s.add(row)
                await s.flush()
                stored = await self.credentials.persist_config(
                    s, row, config, isolation, user_id
                )
                row.config = self.seal(stored)
                await s.flush()
        except IntegrityError:
            raise HTTPException(409, "MCP slug already exists")
        self.runtime.versions[row.id] = row.revision
        await self.refresh_after_change(row, user_id, authorize)
        return await self.get(row.id)

    async def update(self, server_id, values, *, authorize=None, user_id=None):
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
                    row.config = self.seal(stored)
                    row.revision += 1
                    await s.flush()
                # A revision or ownership change must never publish stale tools.
                self.runtime.versions[server_id] = row.revision
                self.runtime.holds.discard(server_id)
                self.cache_store.delete_prefix(server_id + "-")
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
                invalidate_setting_cache(
                    self.db, ["tests:" + server_id, "anonymous_mcp_ids"]
                )
                self.cache_store.delete_prefix(server_id + "-")
                for key in list(self._cache_failures):
                    if key.startswith(server_id + "-"):
                        self._cache_failures.pop(key, None)

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
        return await self.refresh(target["server_id"], target["user_id"],
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
        """Discover an authorized owner's missing directory exactly once."""
        current = self.cached(row, user_id)
        if (
            current["cache_status"] == "ready"
            or current["runtime_status"] == "failed"
            or row.mode == "disabled"
        ):
            return current
        key = (row.id, row.revision, catalog_owner(row.isolation, user_id))
        lock = self.discovery_locks.setdefault(key, asyncio.Lock())
        async with lock:
            row = await self.get(row.id)
            current = self.cached(row, user_id)
            if (
                current["cache_status"] == "ready"
                or current["runtime_status"] == "failed"
                or row.mode == "disabled"
            ):
                return current
            try:
                return await self.refresh(
                    row.id, user_id, authorize=authorize
                )
            except (GatewayError, HTTPException):
                return self.cached(row, user_id)

    async def refresh(self, server_id, user_id=None, *, authorize=None):
        if authorize:
            await authorize()
        row = await self.get(server_id)
        lease, spec = None, None
        had_ready = self.cached(row, user_id)["cache_status"] == "ready"
        try:
            spec = await self.spec(row, user_id, allow_failed=True)
            if authorize:
                await authorize()
            lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
            result = await self.runtime.perform(spec, lease.id, "discover", authorize=authorize, business=False)
            return await self.store_discovery(row, result, user_id, spec=spec)
        except Exception as exc:
            failure = await self.record_refresh_failure(row, exc, user_id, had_ready=had_ready)
            raise failure
        finally:
            if lease:
                await self.runtime.release(lease.id)

    async def warm(self, server_id, user_id=None, *, explicit=False, authorize=None, expected_revision=None):
        row, lease, spec, success = None, None, None, False
        had_ready = False
        try:
            if authorize:
                await authorize()
            row = await self.get(server_id)
            if expected_revision is not None and row.revision != expected_revision:
                return
            if explicit:
                self.runtime.holds.discard(server_id)
            had_ready = self.cached(row, user_id)["cache_status"] == "ready"
            spec = await self.spec(row, user_id, allow_failed=True)
            lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
            result = await self.runtime.perform(spec, lease.id, "discover", authorize=authorize, business=False)
            await self.store_discovery(row, result, user_id, spec=spec)
            success = True
        except Exception as exc:
            if row:
                exc = await self.record_refresh_failure(row, exc, user_id, had_ready=had_ready)
            await self.logs.audit("mcp.start.failed", user_id, {"server_id": server_id, "error": str(exc)})
            raise exc
        finally:
            if lease:
                await self.runtime.release(lease.id, keep_alive=success and explicit)

    @asynccontextmanager
    async def changing(self, server_id):
        was_held = server_id in self.runtime.holds
        await self.stop_for_change(server_id)
        try:
            yield
        finally:
            if not was_held:
                self.runtime.holds.discard(server_id)

    async def stop_for_change(self, server_id):
        try:
            await self.runtime.stop_server(server_id, hold=True, require_idle=True)
        except GatewayError as exc:
            if exc.code == "busy":
                raise HTTPException(409, "MCP has active calls; retry after completion") from exc
            raise

    async def call(self, row, lease, name, arguments, *, user=None, token=None, authorize=None, source="gateway"):
        cached = self.cached(row, user.id if user else None)
        tool = next((x for x in cached["tools"] if x["name"] == name), None)
        event = {"id": str(uuid4()), "user_id": user.id if user else None,
                 "username": user.username if user else "anonymous", "token_id": token.id if token else None,
                 "token_name": token.name if token else "", "mcp_id": row.id, "mcp_name": row.name,
                 "tool_name": name, "arguments": arguments, "source": source, "status": "running",
                 "lease_id": lease.id, "transport": "stdio-bridge" if lease.kind == "bridge" else "http"}
        started = time.monotonic()
        await self.logs.append(event)
        try:
            if tool is None:
                raise GatewayError("tool_not_found", "Tool is absent from the current MCP directory")
            Draft202012Validator(tool.get("inputSchema", {"type": "object"})).validate(arguments)
            if authorize:
                await authorize()
            spec = await self.spec(row, user.id if user else None)
            async def before_dispatch():
                if authorize:
                    await authorize()
                # Startup may have replaced stale schemas before this queued call.
                current_tool = next((x for x in self.cached(row, user.id if user else None)["tools"]
                                     if x["name"] == name), None)
                if current_tool is None:
                    raise GatewayError("tool_not_found", "Tool is absent from the current MCP directory")
                Draft202012Validator(current_tool.get("inputSchema", {"type": "object"})).validate(arguments)
            result = await self.runtime.call(spec, lease.id, name, arguments, authorize=before_dispatch)
            event.update(status="tool_error" if result.get("isError") else "success", result=result)
            return result
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
                await asyncio.shield(self.logs.append(event))
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Final call log write failed; preserving downstream outcome")
