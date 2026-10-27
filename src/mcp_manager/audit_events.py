"""Bounded, secret-free audit envelopes; historical events retain their action."""
import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import unquote

from .diagnostics import SECRET_KEYS

LOGGER = logging.getLogger(__name__)
AUDIT_CONTEXT = ContextVar("mmg_audit_context", default=None)
MAX_DETAILS_BYTES = 16384
COMMON = {"mcp_id", "server_id", "token_id", "owner_id", "user_id", "proposal_id", "lease_id",
          "name", "revision", "result", "count", "job_id", "reason_code", "error_type", "user_agent"}
POLICIES = {
    "identity.login": set(), "identity.login_failed": {"reason_code"}, "identity.logout": set(),
    "identity.register": set(), "identity.session.revoke": {"count"}, "identity.password.change": set(),
    "token.create": {"scope_mode", "enable_resource_tools", "enable_mcp_proposal"},
    "token.update": {"changed_fields", "changes"}, "token.delete": set(), "token.rotate": set(), "token.secret.reveal": set(),
    "user.create": {"role"}, "user.update": {"changed_fields", "changes"}, "user.delete": set(),
    "user.role_change": {"old_role", "new_role"}, "user.batch": {"action", "count", "ids"},
    "mcp.create": set(), "mcp.update": {"changed_fields"}, "mcp.delete": set(),
    "mcp.start": set(), "mcp.stop": set(), "mcp.refresh": {"cache_status"},
    "mcp.start.failed": {"error_type"}, "mcp.export": {"count", "include_secrets"},
    "mcp.credentials.refresh": set(),
    "mcp.proposal.submit": {"count", "ids"}, "mcp.proposal.configure": {"changed_fields"},
    "mcp.proposal.approve": set(), "mcp.proposal.reject": set(), "mcp.proposal.delete": set(),
    "settings.update": {"changes"}, "storage.settings.update": {"changes"},
    "storage.vacuum": {"database", "status", "reclaimed_bytes"},
    "storage.startup": {"database", "status", "reclaimed_bytes"}, "storage.startup.error": {"database"},
    "lease.disconnect": {"count", "disconnected", "released"},
    "logs.delete": {"count"}, "logs.clear": {"count"}, "logs.export": {"format", "filters"},
    "logs.rebuild": {"phase"}, "audit.delete": {"operation_id", "phase", "count", "files", "filters"},
    "audit.clear": {"operation_id", "phase", "count", "files", "filters"},
    "audit.clear_auto": {"operation_id", "phase", "count", "files", "days"},
    "audit.export": {"format", "filters"}, "audit.rebuild": {"phase"},
    "maintenance.error": {"step", "error_type"}, "update.ignore": {"ignored_version"},
}
for _action in ("start", "authorized", "refresh", "client", "reset", "state_invalidated", "cimd_verify", "cimd_fallback"):
    POLICIES["oauth." + _action] = {"registration_mode", "reason", "auth_mode", "code", "status"}
FAILURE_ACTIONS = {"identity.login_failed", "mcp.start.failed", "maintenance.error", "storage.startup.error"}
ACTIONS = [{"action": action, "category": action.split(".")[0]} for action in sorted(POLICIES)]
SENSITIVE = SECRET_KEYS | {"state", "code", "code_challenge", "set-cookie", "set_cookie", "session_id"}
TOKEN_PATTERN = re.compile(r"(?i)(?:bearer\s+[^\s,;]+|mcpm_[a-z0-9_-]+)")
URL_PARAMETER = re.compile(r"([?&#])([^=&#\s]{1,128})=([^&#\s]*)")
URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@")
SENSITIVE_NAMES = {re.sub(r"[^a-z0-9]", "", key.lower()) for key in SENSITIVE}
IDENTIFIER_NAMES = {"tokenid", "ownerid", "userid", "requestid", "targetid", "mcpid", "serverid", "leaseid", "proposalid"}


def sensitive_key(key):
    name = re.sub(r"[^a-z0-9]", "", unquote(str(key)).lower())
    return name not in IDENTIFIER_NAMES and (name in SENSITIVE_NAMES or any(
        part in name for part in ("token", "secret", "password", "authorization", "apikey")))


def scrub_parameter(match):
    key = match.group(2)
    secret = sensitive_key(key) or unquote(key).lower() == "key"
    return match.group(1) + key + "=" + ("[REDACTED]" if secret else match.group(3))


def clean(value, *, depth=0):
    if depth > 8:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        return {str(key)[:128]: "[REDACTED]" if sensitive_key(key)
                or str(key).lower() in {"env", "headers", "env_headers"} else clean(item, depth=depth + 1)
                for key, item in list(value.items())[:200]}
    if isinstance(value, (list, tuple)):
        return [clean(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        value = TOKEN_PATTERN.sub("[REDACTED]", value)
        value = URL_USERINFO.sub(r"\1[REDACTED]@", value)
        return URL_PARAMETER.sub(scrub_parameter, value)[:4096]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return type(value).__name__


def safe_details(action, details, *, historical=False):
    details = details if isinstance(details, dict) else {}
    if not historical:
        allowed = COMMON | POLICIES.get(action, set())
        dropped = set(details) - allowed
        if dropped:
            LOGGER.warning("Dropped unregistered audit fields for %s", action)
        details = {key: value for key, value in details.items() if key in allowed}
    result = clean(details)
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_DETAILS_BYTES:
        return {"truncated": True, "fields": list(result)[:100]}
    return result


def timestamp(value=None):
    stamp = datetime.fromisoformat(value) if value else datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat(timespec="microseconds")


def normalize(event, *, fallback_id=None):
    for key in ("event_id", "timestamp", "action", "user_id", "username", "actor_type",
                "target_type", "target_id", "target_name", "result", "source", "ip", "request_id"):
        if event.get(key) is not None and not isinstance(event[key], str):
            raise TypeError("Audit metadata must be scalar text: " + key)
    if event.get("details") is not None and not isinstance(event["details"], dict):
        raise TypeError("Audit details must be an object")
    action = str(event.get("action") or "unknown")[:128]
    details = safe_details(action, event.get("details"), historical=True)
    raw_result = event.get("result", details.get("result", "unknown"))
    result = {"ready": "success", "needs_reauth": "failure", "error": "failure", "failed": "failure"}.get(
        raw_result, raw_result) if isinstance(raw_result, str) else "unknown"
    if result not in {"success", "failure", "pending", "unknown"}:
        result = "unknown"
    user_id = event.get("user_id")
    target_type = event.get("target_type")
    target_id = event.get("target_id")
    if not target_type:
        for key, kind in (("proposal_id", "proposal"), ("mcp_id", "mcp"), ("server_id", "mcp"),
                          ("token_id", "token"), ("lease_id", "lease"), ("user_id", "user")):
            if details.get(key):
                target_type, target_id = kind, details[key]
                break
    return {
        "event_id": str(event.get("event_id") or fallback_id or uuid.uuid4().hex)[:128],
        "timestamp": timestamp(event.get("timestamp")), "action": action,
        "category": action.split(".")[0], "actor_type": event.get("actor_type") or ("user" if user_id else "system"),
        "user_id": str(user_id)[:128] if user_id else None,
        "username": clean(event.get("username")),
        "target_type": str(target_type)[:64] if target_type else None,
        "target_id": str(target_id)[:128] if target_id else None, "target_name": clean(event.get("target_name")),
        "result": result, "source": str(event.get("source") or "unknown")[:32],
        "ip": str(event.get("ip"))[:64] if event.get("ip") else None,
        "request_id": str(event.get("request_id"))[:128] if event.get("request_id") else None,
        "details": details,
    }


def build_event(action, user_id, details, **context):
    inherited = dict(AUDIT_CONTEXT.get() or {})
    if inherited.get("user_id") != user_id:
        inherited.pop("username", None)
    values = inherited | context
    values.update(action=action, user_id=user_id, details=safe_details(action, details),
                  event_id=uuid.uuid4().hex)
    values.setdefault("source", "system")
    values.setdefault("actor_type", "user" if user_id else "anonymous" if values["source"] == "web" else "system")
    default_result = "failure" if action in FAILURE_ACTIONS else "success"
    values.setdefault("result", details.get("result", default_result) if isinstance(details, dict) else default_result)
    return normalize(values)
