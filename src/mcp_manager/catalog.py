"""Catalog, sealed configuration and revision-scoped capability caches."""
import asyncio
import copy
import hashlib
import json
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import HTTPException
from jsonschema import Draft202012Validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .database import ApiToken, McpServer, SystemSetting, User, now
from .identity import public as public_model
from .identity import revalidate_user
from .runtime import GatewayError, ServerSpec
from .transports import validate_config

SECRET_KEYS = {"password", "token", "access_token", "refresh_token", "client_secret",
               "authorization", "cookie", "api_key", "value"}


def masked(value, parent=""):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if (k.lower() in SECRET_KEYS or parent in {"env", "headers"})
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
        self.oauth = None
        self.tasks = set()
        self._cache_memory = OrderedDict()

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
        result.update(tool_count=len(cache["tools"]), cache_at=cache.get("cache_at"),
                      cache_error=cache.get("cache_error"), cache_status=cache.get("cache_status"),
                      cache_error_code=cache.get("cache_error_code"),
                      cache_attempt_at=cache.get("cache_attempt_at"),
                      auto_disabled=cache.get("auto_disabled", False),
                      auth_type=self.unseal(row.config).get("auth", {}).get("type", "none"),
                      runtime=[x for x in self.runtime.status(user_id=runtime_user_id) if x["server_id"] == row.id])
        if detail:
            result["config"] = masked(self.unseal(row.config))
        return result

    def cache_path(self, row, user_id=None):
        config = self.unseal(row.config)
        scope = user_id if config.get("auth", {}).get("scope") == "user" else "service"
        suffix = hashlib.sha256(str(scope or "anonymous").encode()).hexdigest()[:24]
        return self.cache_dir / (row.id + "-" + str(row.revision) + "-" + suffix + ".json")

    def cached(self, row, user_id=None):
        path = self.cache_path(row, user_id)
        if row.mode == "disabled" and not path.exists():
            # A service-wide disable reason contains no private capability definitions.
            path = self.cache_path(row)
        empty = {"tools": [], "resources": [], "prompts": [], "templates": [],
                 "cache_at": None, "cache_error": None, "cache_error_code": None,
                 "cache_status": "empty", "cache_attempt_at": None}
        if self.runtime.versions.get(row.id, row.revision) != row.revision:
            return empty | {"cache_status": "disabled"}
        def visible(value):
            if row.mode == "disabled" or value["cache_status"] != "ready":
                value = value | {key: [] for key in ("tools", "resources", "prompts", "templates")}
                if row.mode == "disabled" and value["cache_status"] != "error":
                    value["cache_status"] = "disabled"
            return value
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            saved = self._cache_memory.get(path)
            if saved and saved[0] == signature:
                self._cache_memory.move_to_end(path)
                return visible(copy.deepcopy(saved[1]))
            value = empty | json.loads(path.read_text(encoding="utf-8"))
            if value["cache_status"] == "empty" and value["cache_at"]:
                value["cache_status"] = "error" if value["cache_error"] else "ready"
            self._cache_memory[path] = (signature, value)
            if len(self._cache_memory) > 512:
                self._cache_memory.popitem(last=False)
            return visible(copy.deepcopy(value))
        except (OSError, ValueError):
            return visible(empty)

    def save_cache(self, row, value, user_id=None):
        path = self.cache_path(row, user_id)
        temp = path.with_suffix("." + uuid4().hex + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        self._cache_memory.pop(path, None)

    def cache_failure(self, row, exc, user_id=None):
        if (self.runtime.versions.get(row.id, row.revision) != row.revision
                or getattr(exc, "code", "") in {"credential_changed", "revision_changed"}):
            return
        code = getattr(exc, "code", "discovery_failed")
        value = self.cached(row, user_id)
        # Failed discovery is never a usable capability directory.
        value.update(tools=[], resources=[], prompts=[], templates=[], cache_at=None)
        value.update(cache_status="auth_required" if code == "auth_required" else "error",
                     cache_error_code=code, cache_error=str(exc)[:500], cache_attempt_at=now().isoformat())
        self.save_cache(row, value, user_id)

    async def discovery_failed(self, row, exc, user_id=None, *, spec=None):
        # Local admission/auth changes do not prove that the upstream is broken.
        code = getattr(exc, "code", "discovery_failed")
        if isinstance(exc, HTTPException) or code in {
            "credential_changed", "revision_changed", "permission_revoked", "forbidden",
            "lease_expired", "queue_timeout", "stopped", "disabled", "shutting_down",
        }:
            return
        if spec and spec.credential_owner and self.runtime.credential_versions.get(
                (spec.id, spec.credential_owner), spec.credential_scope) != spec.credential_scope:
            return
        async with self.db.locked() as session:
            current = await session.get(McpServer, row.id)
            if not current or current.revision != row.revision or current.mode == "disabled":
                return
            current.mode = "disabled"
            current.revision += 1
            failure = {
                "tools": [], "resources": [], "prompts": [], "templates": [],
                "cache_status": "error", "cache_error_code": code,
                "cache_error": str(exc)[:500], "cache_attempt_at": now().isoformat(),
                "cache_at": None, "auto_disabled": True,
            }
            self.save_cache(current, failure, user_id)
            if self.cache_path(current, user_id) != self.cache_path(current):
                self.save_cache(current, failure)
            await session.flush()
        self.runtime.versions[row.id] = current.revision
        # The caller is outside the SDK owner task. Stop only the failed revision;
        # an administrator may already be testing a newer configuration.
        await self.runtime.stop_server(row.id, revision=row.revision)

    def mark_refreshing(self, row, user_id=None):
        if row.mode != "disabled" and self.runtime.versions.get(row.id, row.revision) == row.revision:
            self.save_cache(row, {"tools": [], "resources": [], "prompts": [], "templates": [],
                                 "cache_status": "refreshing", "cache_attempt_at": now().isoformat()})

    async def connection_starting(self, spec):
        if spec.id.startswith("diagnose-"):
            return
        row = await self.get(spec.id)
        if row.revision != spec.revision or row.mode == "disabled":
            raise GatewayError("revision_changed", "MCP configuration changed before startup")
        user_id = spec.credential_owner if spec.credential_owner not in {"", "service"} else None
        self.mark_refreshing(row, user_id)

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
            await self.discovery_failed(row, exc, user_id, spec=spec)

    async def store_discovery(self, row, result, user_id=None, *, spec=None):
        current = await self.get(row.id)
        if current.revision != row.revision or current.mode == "disabled":
            raise GatewayError("revision_changed", "MCP configuration changed during discovery")
        if spec is not None:
            self.runtime._available(spec)
        result = result | {"cache_at": now().isoformat(), "cache_attempt_at": now().isoformat(),
                           "cache_error": None, "cache_error_code": None, "cache_status": "ready"}
        self.save_cache(row, result, user_id)
        return result

    async def connection_ready(self, spec, connection):
        # Diagnostics are not saved services.
        if spec.id.startswith("diagnose-"):
            return
        row = await self.get(spec.id)
        if row.revision != spec.revision:
            raise GatewayError("revision_changed", "MCP configuration changed during startup")
        user_id = spec.credential_owner if spec.credential_owner != "service" else None
        try:
            await self.store_discovery(row, await connection.discover(), user_id, spec=spec)
        except Exception as exc:
            self.cache_failure(row, exc, user_id)
            raise

    async def refresh_after_change(self, row, user_id=None, authorize=None):
        if row.mode == "disabled":
            self.save_cache(row, {"tools": [], "resources": [], "prompts": [], "templates": [],
                                  "cache_status": "disabled"})
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

    async def spec(self, row, user_id=None):
        if row.mode == "disabled":
            raise GatewayError("disabled", "MCP is disabled; repair configuration or authorization, then enable it")
        config = self.unseal(row.config)
        credential_scope = ""
        credential_owner = ""
        isolation = row.isolation
        if config.get("auth", {}).get("type") == "oauth":
            if config["auth"].get("scope") == "user":
                if not user_id:
                    raise GatewayError("auth_required", "Personal OAuth requires a signed-in user")
                isolation = "user"
            if self.oauth:
                auth = await self.oauth.credentials(row, user_id)
                # OAuth token responses use "scope" for provider permissions.
                # Keep gateway ownership and configuration under local control.
                config["auth"].update({key: auth[key] for key in
                    ("access_token", "token_type", "expires_at") if key in auth})
                if "scope" in auth:
                    config["auth"]["granted_scope"] = auth["scope"]
                credential_scope = hashlib.sha256(auth.get("access_token", "").encode()).hexdigest()[:16]
                credential_owner = user_id if config["auth"].get("scope") == "user" else "service"
                self.runtime.credential_versions[(row.id, credential_owner)] = credential_scope
        return ServerSpec(row.id, row.transport, config, row.mode, isolation, row.revision,
                          credential_scope, credential_owner)

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
        validate_config(transport, config)
        mode, isolation = values.get("mode", "lazy"), values.get("isolation", "service")
        if mode not in {"eager", "lazy", "disabled"} or isolation not in {"service", "user", "session"}:
            raise ValueError("Invalid mode or isolation")
        name = values.get("name", "").strip()
        if not name or len(name) > 128:
            raise ValueError("Name must contain 1–128 characters")
        generated = (re.sub(r"[^a-zA-Z0-9-]+", "_", name).strip("_") or "mcp")[:57]
        slug = values.get("slug") or generated + "_" + uuid4().hex[:6]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", slug) or "__" in slug:
            raise ValueError("Slug must contain 1–64 ASCII letters, digits, hyphens or single underscores")
        row = McpServer(id=str(uuid4()), name=name, slug=slug, description=values.get("description", ""),
                        tags=values.get("tags", []), transport=transport, mode=mode, isolation=isolation,
                        config=self.seal(config))
        try:
            async with self.db.locked() as s:
                if authorize:
                    await authorize(s)
                s.add(row)
                await s.flush()
        except IntegrityError:
            raise HTTPException(409, "MCP slug already exists")
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
            config = restore(values.get("config", self.unseal(old.config)), self.unseal(old.config))
            transport = values.get("transport", old.transport)
            transport = "streamable-http" if transport == "http" else transport
            validate_config(transport, config)
            if values.get("mode", old.mode) not in {"eager", "lazy", "disabled"}:
                raise ValueError("Invalid mode")
            if values.get("isolation", old.isolation) not in {"service", "user", "session"}:
                raise ValueError("Invalid isolation")
            if authorize:
                await authorize()
            async with self.changing(server_id):
                async with self.db.locked() as s:
                    if authorize:
                        await authorize(s)
                    row = await s.get(McpServer, server_id)
                    for key in ("name", "description", "tags", "mode", "isolation"):
                        if key in values:
                            setattr(row, key, values[key])
                    row.transport, row.config, row.revision = transport, self.seal(config), row.revision + 1
                    await s.flush()
                # Every new revision must discover its own capabilities before publication.
                self.runtime.versions[server_id] = row.revision
                self.runtime.holds.discard(server_id)
                await self.refresh_after_change(row, user_id, authorize)
                row = await self.get(row.id)
                for target in await self.refresh_targets(server_id):
                    if target["user_id"] and target["user_id"] != user_id:
                        async def owner_authorize(target=target):
                            await self.authorize_target(target)
                        self.schedule_warm(row, owner_authorize, target["user_id"])
                return row

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
                    await s.execute(delete(SystemSetting).where(
                        SystemSetting.key.startswith("oauth:" + server_id + ":", autoescape=True)))
                    await s.execute(delete(SystemSetting).where(SystemSetting.key == "tests:" + server_id))
                    for model in (User, ApiToken):
                        for principal in (await s.scalars(select(model))).all():
                            if server_id in principal.mcp_ids:
                                principal.mcp_ids = [item for item in principal.mcp_ids if item != server_id]
                    anonymous = await s.get(SystemSetting, "anonymous_mcp_ids")
                    if anonymous:
                        anonymous.value = [item for item in anonymous.value if item != server_id]
                for path in self.cache_dir.glob(server_id + "-*.json"):
                    path.unlink(missing_ok=True)

    async def refresh_targets(self, server_id=None):
        targets = []
        for row in await self.rows(ids=[server_id] if server_id else None):
            if row.mode == "disabled":
                continue
            auth = self.unseal(row.config).get("auth", {})
            if auth.get("type") == "oauth" and auth.get("scope") == "user":
                prefix = "oauth:" + row.id + ":"
                async with self.db.session() as session:
                    credentials = (await session.scalars(select(SystemSetting).where(
                        SystemSetting.key.startswith(prefix, autoescape=True)))).all()
                    for credential in credentials:
                        user_id = credential.key[len(prefix):]
                        user = await session.get(User, user_id)
                        if (credential.value and user and not user.disabled and
                                (user.role == "admin" or user.scope_mode == "all" or row.id in user.mcp_ids)):
                            targets.append({"server_id": row.id, "user_id": user_id})
            else:
                targets.append({"server_id": row.id, "user_id": None})
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

    async def refresh(self, server_id, user_id=None, *, authorize=None):
        if authorize:
            await authorize()
        row = await self.get(server_id)
        lease, spec = None, None
        self.mark_refreshing(row, user_id)
        try:
            spec = await self.spec(row, user_id)
            if authorize:
                await authorize()
            lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
            result = await self.runtime.perform(spec, lease.id, "discover", authorize=authorize, business=False)
            return await self.store_discovery(row, result, user_id, spec=spec)
        except Exception as exc:
            await self.discovery_failed(row, exc, user_id, spec=spec)
            raise
        finally:
            if lease:
                await self.runtime.release(lease.id)

    async def warm(self, server_id, user_id=None, *, explicit=False, authorize=None, expected_revision=None):
        row, lease, spec, success = None, None, None, False
        try:
            if authorize:
                await authorize()
            row = await self.get(server_id)
            if expected_revision is not None and row.revision != expected_revision:
                return
            if explicit:
                self.runtime.holds.discard(server_id)
            self.mark_refreshing(row, user_id)
            spec = await self.spec(row, user_id)
            lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
            result = await self.runtime.perform(spec, lease.id, "discover", authorize=authorize, business=False)
            await self.store_discovery(row, result, user_id, spec=spec)
            success = True
        except Exception as exc:
            if row:
                await self.discovery_failed(row, exc, user_id, spec=spec)
            await self.logs.audit("mcp.start.failed", user_id, {"server_id": server_id, "error": str(exc)})
            raise
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
                await self.refresh(row.id, user.id if user else None, authorize=authorize)
                tool = next((x for x in self.cached(row, user.id if user else None)["tools"]
                             if x["name"] == name), None)
                if tool is None:
                    raise GatewayError("tool_not_found", "Tool is absent from the refreshed MCP directory")
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
            if getattr(exc, "code", "") in {"startup_failed", "startup_timeout", "auth_required", "connection_error"}:
                await self.discovery_failed(row, exc, user.id if user else None)
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
