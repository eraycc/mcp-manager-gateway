"""Explicit operation boundaries with request metadata, never request/response bodies."""
import functools
import inspect
import uuid
from contextvars import ContextVar

from .audit_events import AUDIT_CONTEXT

OPERATION = ContextVar("mmg_audit_operation", default=None)


class AuditContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        context = {"request_id": uuid.uuid4().hex,
                   "source": "gateway" if scope["path"].startswith(("/mcp", "/gateway/")) else "web",
                   "ip": scope.get("client", (None,))[0]}
        scope.setdefault("state", {})["audit_context"] = context
        token = AUDIT_CONTEXT.set(context)
        try:
            await self.app(scope, receive, send)
        finally:
            AUDIT_CONTEXT.reset(token)


def bind_actor(request, user):
    context = dict(getattr(request.state, "audit_context", {}))
    context.update(user_id=user.id, username=user.username, actor_type="user")
    AUDIT_CONTEXT.set(context)
    request.state.audit_actor = context


def enrich(**values):
    context = OPERATION.get()
    if context is not None:
        context.update(values)


def audited(action, *, target_type=None, id_param=None):
    """Emit after the handler and its transaction contexts have completed."""
    def decorate(function):
        signature = inspect.signature(function)
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            bound = signature.bind_partial(*args, **kwargs).arguments
            request = bound.get("request")
            if request is None or not getattr(request.app.state, "logs", None):
                return await function(*args, **kwargs)
            operation = {"details": {}}
            marker = OPERATION.set(operation)
            user = bound.get("user")
            actor = dict(getattr(request.state, "audit_actor", {}))
            if hasattr(user, "id"):
                actor.update(user_id=user.id, username=user.username, actor_type="user")
            metadata = dict(getattr(request.state, "audit_context", {})) | actor
            data = bound.get("data")
            if action in {"identity.login", "identity.register"} and hasattr(data, "username"):
                metadata["username"] = data.username
            if hasattr(data, "model_fields_set"):
                if action in {"token.update", "user.update"}:
                    operation["details"]["changed_fields"] = sorted(data.model_fields_set)
                if action == "user.create":
                    operation["details"]["role"] = data.role
                if action == "user.batch":
                    operation["details"].update(action=data.action, count=len(data.ids), ids=data.ids)
                if action == "token.create":
                    operation["details"].update({key: getattr(data, key) for key in
                        ("scope_mode", "enable_resource_tools", "enable_mcp_proposal")})
            if action == "mcp.proposal.configure" and isinstance(data, dict):
                operation["details"]["changed_fields"] = sorted(data)
            if action.startswith("identity."):
                operation["details"]["user_agent"] = (request.headers.get("user-agent") or "")[:512]
            identity = bound.get(id_param) if id_param else None
            if target_type == "user" and action in {"identity.logout", "identity.session.revoke"}:
                identity = metadata.get("user_id")
            outcome = None
            try:
                response = await function(*args, **kwargs)
                if isinstance(response, dict):
                    identity = identity or response.get("id")
                    operation.setdefault("target_name", response.get("name") or response.get("username"))
                    failed = response.get("ok") is False or response.get("cache_status") in {"error", "auth_required"}
                    outcome = "failure" if failed else "success"
                else:
                    outcome = "success" if getattr(response, "status_code", 200) < 400 else "failure"
                return response
            except Exception as exc:
                outcome = "failure"
                operation["details"].update(error_type=type(exc).__name__, reason_code=str(getattr(exc, "status_code", "operation_failed")))
                raise
            finally:
                try:
                    metadata.update(getattr(request.state, "audit_actor", {}))
                    user_id = metadata.pop("user_id", None)
                    event_action = "identity.login_failed" if action == "identity.login" and outcome == "failure" else action
                    await request.app.state.logs.audit(event_action, user_id, operation["details"],
                        **metadata, target_type=target_type, target_id=identity,
                        target_name=operation.get("target_name"), result=outcome or "unknown")
                finally:
                    OPERATION.reset(marker)
        return wrapped
    return decorate
