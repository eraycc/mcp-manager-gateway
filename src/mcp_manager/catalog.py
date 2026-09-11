"""Catalog, sealed configuration and revision-scoped capability caches."""
import asyncio
import copy
import hashlib
import json
import re
import time
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
        self.locks = {}
        self.oauth = None
        self.tasks = set()

    def schedule_warm(self, row, authorize=None):
        task = asyncio.create_task(self.warm(row.id, authorize=authorize, expected_revision=row.revision))
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

    async def rows(self):
        async with self.db.session() as s:
            return list((await s.scalars(select(McpServer).order_by(McpServer.created_at.desc()))).all())

    def public(self, row, *, detail=False, user_id=None):
        result = {k: public_model(row)[k] for k in ("id", "slug", "name", "description", "tags", "transport",
                                             "mode", "isolation", "revision", "created_at")}
        cache = self.cached(row, user_id)
        result.update(tool_count=len(cache["tools"]), cache_at=cache.get("cache_at"),
                      cache_error=cache.get("cache_error"),
                      runtime=[x for x in self.runtime.status(user_id=user_id) if x["server_id"] == row.id])
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
        empty = {"tools": [], "resources": [], "prompts": [], "templates": [],
                 "cache_at": None, "cache_error": None}
        try:
            return empty | json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty

    def save_cache(self, row, value, user_id=None):
        path = self.cache_path(row, user_id)
        temp = path.with_suffix("." + uuid4().hex + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    async def spec(self, row, user_id=None):
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
                config["auth"] = config["auth"] | auth
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

    async def create(self, values, *, authorize=None):
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
        if transport == "rest":
            from .transports import RestConnection
            self.save_cache(row, (await RestConnection(config, None).discover()) | {"cache_at": now().isoformat()})
        if mode == "eager" and isolation == "service":
            self.schedule_warm(row, authorize)
        return row

    async def update(self, server_id, values, *, authorize=None):
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
            await self.runtime.stop_server(server_id, hold=True)
            if any(x["server_id"] == server_id and x["in_flight"] for x in self.runtime.status()):
                raise HTTPException(409, "MCP is draining active calls; retry after completion")
            async with self.db.locked() as s:
                if authorize:
                    await authorize(s)
                row = await s.get(McpServer, server_id)
                for key in ("name", "description", "tags", "mode", "isolation"):
                    if key in values:
                        setattr(row, key, values[key])
                row.transport, row.config, row.revision = transport, self.seal(config), row.revision + 1
                await s.flush()
            if config == self.unseal(old.config) and transport == old.transport:
                for source in self.cache_dir.glob(old.id + "-" + str(old.revision) + "-*.json"):
                    target = self.cache_dir / source.name.replace(
                        old.id + "-" + str(old.revision) + "-", row.id + "-" + str(row.revision) + "-", 1)
                    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            self.runtime.versions[server_id] = row.revision
            self.runtime.holds.discard(server_id)
            if row.transport == "rest":
                from .transports import RestConnection
                self.save_cache(row, (await RestConnection(config, None).discover()) | {"cache_at": now().isoformat()})
            if row.mode == "eager" and row.isolation == "service":
                self.schedule_warm(row, authorize)
            return row

    async def delete(self, server_id, *, authorize=None):
        async with self.locks.setdefault(server_id, asyncio.Lock()):
            if authorize:
                await authorize()
            await self.runtime.stop_server(server_id, hold=True)
            if any(x["server_id"] == server_id and x["in_flight"] for x in self.runtime.status()):
                raise HTTPException(409, "MCP is draining active calls")
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

    async def refresh_targets(self):
        targets = []
        for row in await self.rows():
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

    async def refresh_target(self, target):
        async def authorize():
            if target["user_id"]:
                async with self.db.session() as session:
                    user = await session.get(User, target["user_id"])
                    if (not user or user.disabled or (user.role != "admin" and user.scope_mode != "all"
                                                     and target["server_id"] not in user.mcp_ids)):
                        raise GatewayError("permission_revoked", "Scheduled refresh owner lost access")
        return await self.refresh(target["server_id"], target["user_id"], authorize=authorize)

    async def refresh(self, server_id, user_id=None, *, authorize=None):
        if authorize:
            await authorize()
        row = await self.get(server_id)
        spec = await self.spec(row, user_id)
        if authorize:
            await authorize()
        lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
        try:
            result = await self.runtime.discover(spec, lease.id)
            result.update(cache_at=now().isoformat(), cache_error=None)
            self.save_cache(row, result, user_id)
            return result
        except Exception as exc:
            result = self.cached(row, user_id)
            result["cache_error"] = type(exc).__name__ + ": " + str(exc)[:500]
            self.save_cache(row, result, user_id)
            raise
        finally:
            await self.runtime.release(lease.id)

    async def warm(self, server_id, user_id=None, *, explicit=False, authorize=None, expected_revision=None):
        try:
            if authorize:
                await authorize()
            row = await self.get(server_id)
            if expected_revision is not None and row.revision != expected_revision:
                return
            if explicit:
                self.runtime.holds.discard(server_id)
            lease = self.runtime.create_lease(user_id or "system", "maintenance", kind="maintenance")
            try:
                spec = await self.spec(row, user_id)
                if authorize:
                    await authorize()
                await self.runtime.discover(spec, lease.id)
            finally:
                # Eager processes survive maintenance release; lazy ones need an explicit owner.
                if row.mode == "eager":
                    await self.runtime.release(lease.id)
                else:
                    lease.kind = "retention"
        except Exception as exc:
            await self.logs.audit("mcp.start.failed", user_id, {"server_id": server_id, "error": str(exc)})
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
            if tool:
                Draft202012Validator(tool.get("inputSchema", {"type": "object"})).validate(arguments)
            if authorize:
                await authorize()
            spec = await self.spec(row, user.id if user else None)
            result = await self.runtime.call(spec, lease.id, name, arguments, authorize=authorize)
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
