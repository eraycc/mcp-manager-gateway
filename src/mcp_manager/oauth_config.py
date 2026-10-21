"""OAuth registration settings, binding and public metadata without network I/O."""
import hashlib
import ipaddress
import json
from urllib.parse import urlsplit

from .runtime import GatewayError

CLIENT_METADATA_PATH = "/oauth/client-metadata.json"
DEFAULT_CALLBACK_PATH = "/api/v1/oauth/callback"
CALLBACK_PATHS = (DEFAULT_CALLBACK_PATH, "/callback")
TOKEN_FIELDS = {"access_token", "refresh_token", "expires_at", "expires_in", "granted_scope", "id_token", "token_type"}


def local_host(host):
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def oauth_url(value, *, public=False):
    if not isinstance(value, str):
        raise TypeError("OAuth URL must be a string")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("OAuth URL cannot contain whitespace, control characters or backslashes")
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.scheme not in {"https", "http"}):
        raise ValueError("OAuth URL must be an absolute HTTP(S) URL without credentials or fragment")
    _ = parsed.port
    loopback = parsed.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not loopback:
        raise ValueError("OAuth requires HTTPS, except for local loopback addresses")
    if public and (parsed.scheme != "https" or local_host(parsed.hostname) or "." not in parsed.hostname):
        raise ValueError("CIMD requires a stable, publicly reachable HTTPS PUBLIC_URL")
    return parsed


def callback_path(auth):
    path = auth.get("callback_path", DEFAULT_CALLBACK_PATH)
    if not isinstance(path, str) or path not in CALLBACK_PATHS:
        raise ValueError("OAuth callback_path must be /api/v1/oauth/callback or /callback")
    return path


def callback_uri(public_url, auth=None):
    """Build a same-origin callback using the explicitly configured handler path."""
    try:
        path = callback_path(auth or {})
    except ValueError as exc:
        raise GatewayError("oauth_configuration_required", str(exc)) from None
    return public_url.rstrip("/") + path


def client_metadata(public_url):
    try:
        parsed = oauth_url(public_url, public=True)
        if parsed.query:
            raise ValueError("PUBLIC_URL cannot contain a query")
    except (ValueError, TypeError) as exc:
        raise GatewayError("oauth_configuration_required", str(exc)) from None
    identity = public_url.rstrip("/") + CLIENT_METADATA_PATH
    return {"client_id": identity, "client_name": "MCP Manager Gateway",
            "redirect_uris": [callback_uri(public_url, {"callback_path": path}) for path in CALLBACK_PATHS],
            "grant_types": ["authorization_code"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}


REGISTRATION_MODES = {"pre_registration", "prefer_cimd_fallback_dcr", "cimd_only", "dcr_only"}


def registration_policy(auth):
    """Interpret legacy records without silently enabling DCR for old automatic clients."""
    mode = auth.get("auth_mode")
    if mode == "manual":
        return "pre_registration"
    if mode == "auto":
        return "prefer_cimd_fallback_dcr" if auth.get("allow_dcr") is True else "cimd_only"
    if mode == "dcr":
        return "dcr_only"
    if mode is None:
        # Historical endpoint/token-only OAuth was pre-registered.
        return "pre_registration"
    return mode


def normalize_registration(auth):
    """Canonicalize explicit settings on save; keep historical token-only records valid."""
    value = dict(auth)
    if value.get("auth_mode") is not None:
        value["auth_mode"] = registration_policy(value)
        value.pop("allow_dcr", None)
        if allows_dcr(value):
            value.setdefault("client_name", "Codex")
    return value


def allows_dcr(auth):
    return registration_policy(auth) in {"prefer_cimd_fallback_dcr", "dcr_only"}


def configuration_fingerprint(auth):
    value = {key: item for key, item in auth.items() if key not in TOKEN_FIELDS}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def registration_fingerprint(auth, resource, redirect_uri):
    # CredentialStore removes config_isolation from the sealed auth_config.
    base = comparable_settings(auth)
    return configuration_fingerprint({"auth": base, "resource": resource, "redirect_uri": redirect_uri})


def registration_matches(registration, auth, resource, redirect_uri):
    """Accept equivalent old DCR bindings while still checking resource and callback."""
    expected = registration_fingerprint(auth, resource, redirect_uri)
    if registration.get("config_fingerprint") == expected:
        return True
    if registration_policy(auth) == "dcr_only" and registration.get("mode") == "dcr":
        # Old forced DCR records used the auto+allow_dcr representation in fingerprints.
        legacy = auth | {"auth_mode": "auto", "allow_dcr": True}
        return registration.get("config_fingerprint") == registration_fingerprint(legacy, resource, redirect_uri)
    return False


def validate_oauth(auth, transport):
    callback_path(auth)
    mode = auth.get("auth_mode")
    if mode is None:
        # Preserve historical token-only / manually configured OAuth records.
        for key in ("authorization_url", "token_url"):
            value = auth.get(key, "")
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("OAuth " + key + " must be an HTTP(S) URL without credentials")
        return
    mode = registration_policy(auth)
    if not isinstance(mode, str) or mode not in REGISTRATION_MODES:
        raise ValueError("OAuth auth_mode must be pre_registration, prefer_cimd_fallback_dcr, cimd_only or dcr_only")
    if mode != "pre_registration" and transport not in {"streamable-http", "http", "sse"}:
        raise ValueError("CIMD/DCR is supported only for remote HTTP/SSE MCP servers")
    if "allow_dcr" in auth and type(auth["allow_dcr"]) is not bool:
        raise ValueError("OAuth allow_dcr must be a boolean")
    if mode == "pre_registration":
        if not isinstance(auth.get("client_id"), str) or not auth["client_id"].strip():
            raise ValueError("OAuth Client ID is required for pre-registration")
        if auth.get("client_name") or auth.get("allow_dcr"):
            raise ValueError("OAuth manual mode cannot contain DCR fields")
    else:
        if auth.get("client_id") or auth.get("client_secret"):
            raise ValueError("OAuth automatic mode cannot contain manual Client ID/Secret")
        name = auth.get("client_name", "Codex")
        if allows_dcr(auth) and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 128):
            raise ValueError("OAuth DCR client_name is required (1..128 characters)")
        # CIMD-only ignores a retained display name; no client_name is sent in authorization.
    if "client_secret" in auth and not isinstance(auth["client_secret"], str):
        raise ValueError("OAuth Client Secret must be a string")
    method = auth.get("token_endpoint_auth_method")
    if method is not None and (not isinstance(method, str) or method not in {"none", "client_secret_basic", "client_secret_post"}):
        raise ValueError("Unsupported OAuth token_endpoint_auth_method")
    if method == "none" and auth.get("client_secret"):
        raise ValueError("OAuth none authentication cannot send a Client Secret")
    if mode == "pre_registration" and method in {"client_secret_basic", "client_secret_post"} and not auth.get("client_secret"):
        raise ValueError("OAuth client_secret authentication requires a Client Secret")
    for key in ("authorization_url", "token_url", "registration_url", "issuer_url"):
        if auth.get(key):
            parsed = oauth_url(auth[key])
            if key == "issuer_url" and parsed.query:
                raise ValueError("OAuth issuer URL cannot contain a query")
    scopes = auth.get("scopes", [])
    if not isinstance(scopes, (list, str)) or isinstance(scopes, list) and not all(isinstance(x, str) for x in scopes):
        raise ValueError("OAuth scopes must be strings")


def comparable_settings(auth):
    """Treat explicit defaults as equivalent to legacy pre-registration settings."""
    value = {key: item for key, item in auth.items() if key not in TOKEN_FIELDS and key != "config_isolation"}
    if value.get("callback_path") == DEFAULT_CALLBACK_PATH:
        value.pop("callback_path")
    policy = registration_policy(value)
    # Retain legacy fingerprint representations for equivalent manual/auto configurations.
    value["auth_mode"] = {"pre_registration": "manual", "prefer_cimd_fallback_dcr": "auto",
                          "cimd_only": "auto"}.get(policy, policy)
    value.pop("allow_dcr", None)
    if policy == "prefer_cimd_fallback_dcr":
        value["allow_dcr"] = True
    if policy in {"prefer_cimd_fallback_dcr", "dcr_only"}:
        value.setdefault("client_name", "Codex")
    if policy == "cimd_only":
        value.pop("client_name", None)
    for key in ("client_secret", "client_name", "issuer_url", "registration_url", "authorization_url", "token_url", "scopes", "allow_dcr"):
        if not value.get(key):
            value.pop(key, None)
    if isinstance(value.get("scopes"), str):
        value["scopes"] = value["scopes"].split()
    if value.get("auth_mode") == "manual":
        value.setdefault("token_endpoint_auth_method", "client_secret_post" if value.get("client_secret") else "none")
    return value
