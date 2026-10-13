"""One bounded redaction boundary for persisted and public downstream failures."""
import re
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

from .runtime import GatewayError

SECRET_KEYS = {
    "password", "current_password", "secret", "secret_key", "database_url",
    "token", "access_token", "refresh_token", "client_secret",
    "authorization", "cookie", "api_key", "value",
}

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



def diagnostic_secrets(value, parent="", sensitive=False):
    values = []
    sensitive = sensitive or parent in {"env", "headers", "env_headers"} or parent in SECRET_KEYS
    if isinstance(value, dict):
        for key, item in value.items():
            values.extend(diagnostic_secrets(item, key.lower(), sensitive))
    elif isinstance(value, list):
        for item in value:
            values.extend(diagnostic_secrets(item, parent, sensitive))
    elif value and isinstance(value, (str, int, float)):
        if sensitive:
            values.append(str(value))
        if isinstance(value, str) and "://" in value:
            try:
                url = urlsplit(value)
                values.extend(unquote(item) for item in (url.username, url.password) if item)
                for key, item in parse_qsl(url.query, keep_blank_values=True):
                    if item and (key.lower() in SECRET_KEYS or any(
                        part in key.lower() for part in ("token", "secret", "key", "password")
                    )):
                        values.append(item)
            except ValueError:
                pass
    return list({encoded for value in values for encoded in (value, quote(value, safe=""), quote_plus(value))})


def safe_diagnostic_error(exc, *, config, max_chars=4000):
    sanitized = sanitize_diagnostic_error(exc, diagnostic_secrets(config))
    return {
        "code": getattr(exc, "code", "diagnosis_failed"),
        "message": str(sanitized)[:max_chars], "scope": "downstream",
        **sanitized.details,
    }


def safe_gateway_error(exc, *, config):
    safe = safe_diagnostic_error(exc, config=config)
    if isinstance(exc, GatewayError):
        # Keep runtime failure identity/recording markers; discard untrusted detail fields.
        exc.args = (safe["message"],)
        secrets = diagnostic_secrets(config)

        def scrub(value, depth=0):
            if depth >= 8:
                return "[TRUNCATED]"
            if isinstance(value, dict):
                return {str(key): scrub(item, depth + 1) for key, item in list(value.items())[:50]}
            if isinstance(value, list):
                return [scrub(item, depth + 1) for item in value[:100]]
            if isinstance(value, str):
                return str(sanitize_diagnostic_error(ValueError(value), secrets))
            return value if value is None or isinstance(value, (bool, int, float)) else str(value)[:500]

        exc.details = scrub(exc.details) | {
            key: value for key, value in safe.items() if key not in {"code", "message"}
        }
        return exc
    from fastapi import HTTPException
    if isinstance(exc, HTTPException):
        return HTTPException(exc.status_code, safe["message"], headers=exc.headers)
    return GatewayError(safe["code"], safe["message"], {
        key: value for key, value in safe.items() if key not in {"code", "message"}
    })
