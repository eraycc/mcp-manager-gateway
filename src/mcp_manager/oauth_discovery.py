"""Bounded, credential-free OAuth metadata discovery and public-client registration."""
import asyncio
import json
from urllib.parse import urlsplit, urlunsplit
from urllib.request import parse_http_list, parse_keqv_list

import httpx

from .oauth_config import local_host, oauth_url
from .runtime import GatewayError

REQUEST_TIMEOUT = 8
MAX_JSON_BYTES = 262144
SAFE_ERRORS = {"invalid_client", "invalid_grant", "invalid_request", "unauthorized_client",
               "unsupported_grant_type", "invalid_scope", "access_denied", "server_error",
               "temporarily_unavailable", "invalid_redirect_uri", "invalid_client_metadata",
               "invalid_software_statement", "unapproved_software_statement"}


class Unavailable(Exception):
    pass


def http_client():
    from .oauth_http import OAuthHTTPTransport

    return httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False, transport=OAuthHTTPTransport())


def canonical_resource(value):
    parsed = oauth_url(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def checked_url(value, resource):
    try:
        parsed = oauth_url(value)
        target = urlsplit(resource)
        if local_host(parsed.hostname) and not local_host(target.hostname):
            raise ValueError("Public MCP metadata cannot redirect OAuth requests into a local network")
        return parsed
    except (ValueError, TypeError) as exc:
        raise GatewayError("oauth_metadata_invalid", str(exc)) from None


def challenge_values(headers):
    result = {}
    bearer = False
    for item in parse_http_list(headers.get("www-authenticate", "")):
        prefix = item.split(" ", 1)
        if len(prefix) == 2 and "=" not in prefix[0]:
            bearer = prefix[0].lower() == "bearer"
            item = prefix[1]
        if bearer and "=" in item:
            values = parse_keqv_list([item])
            for key in ("resource_metadata", "scope"):
                if key in values and key not in result:
                    result[key] = values[key]
    return result


def registration_error(status, value=None):
    code = value.get("error") if isinstance(value, dict) else None
    detail = "，服务器错误：" + code if isinstance(code, str) and code in SAFE_ERRORS else ""
    advice = ("回调地址被服务器拒绝，请检查回调路径及服务器允许列表；填写授权或 Token 端点不会改变回调地址。"
              if code == "invalid_redirect_uri" else
              "请检查客户端名称、注册权限或回调地址，或切换预注册并手动填写 Client ID。")
    return GatewayError("oauth_registration_failed",
                       "目标服务器拒绝了 DCR 注册（HTTP " + str(status) + detail + "）。" + advice)


async def json_request(client, url, resource, *, data=None):
    checked_url(url, resource)
    status = None
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT), client.stream(
            "POST" if data is not None else "GET", url, json=data,
            extensions={"oauth_public_only": not local_host(urlsplit(resource).hostname)},
        ) as response:
            status = response.status_code
            if status in {404, 405, 501} and data is None:
                raise Unavailable
            if not 200 <= status < 300 and data is None:
                if 300 <= status < 400:
                    raise GatewayError("oauth_metadata_invalid", "OAuth metadata redirects are not permitted")
                raise Unavailable
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_JSON_BYTES:
                    if data is not None and not 200 <= status < 300:
                        raise registration_error(status)
                    raise GatewayError("oauth_metadata_invalid", "OAuth response exceeds 256 KiB")
    except (httpx.HTTPError, TimeoutError):
        if data is not None:
            if status is not None and not 200 <= status < 300:
                raise registration_error(status) from None
            raise GatewayError("oauth_registration_unknown",
                               "DCR 注册结果无法确认，请核对服务端记录后手动重试；本次不会自动重试") from None
        raise Unavailable from None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        if data is not None and not 200 <= status < 300:
            raise registration_error(status) from None
        raise GatewayError("oauth_metadata_invalid", "OAuth response is not valid JSON") from None
    if not 200 <= status < 300:
        raise registration_error(status, value)
    if not isinstance(value, dict):
        raise GatewayError("oauth_metadata_invalid", "OAuth response must be a JSON object")
    return value


async def discover(resource, auth):
    resource = canonical_resource(resource)
    parsed = urlsplit(resource)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    challenge = {}
    async with http_client() as client:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), client.stream(
                "GET", resource, headers={"Accept": "application/json"},
                extensions={"oauth_public_only": not local_host(parsed.hostname)},
            ) as response:
                if response.status_code == 401:
                    challenge = challenge_values(response.headers)
                elif 300 <= response.status_code < 400:
                    raise GatewayError("oauth_metadata_invalid", "MCP discovery redirects are not permitted")
        except (httpx.HTTPError, TimeoutError):
            pass
        candidates = ([challenge["resource_metadata"]] if challenge.get("resource_metadata") else
                      list(dict.fromkeys([origin + "/.well-known/oauth-protected-resource" + parsed.path,
                                          origin + "/.well-known/oauth-protected-resource"])))
        protected = None
        for url in candidates:
            try:
                protected = await json_request(client, url, resource)
                break
            except Unavailable:
                continue
        issuer = auth.get("issuer_url") or origin
        if protected is not None:
            try:
                if canonical_resource(protected.get("resource", "")) != resource:
                    raise ValueError("OAuth resource metadata does not match the MCP resource")
                servers = protected.get("authorization_servers")
                if not isinstance(servers, list) or not servers or not all(isinstance(x, str) for x in servers):
                    raise ValueError("OAuth resource metadata has no authorization servers")
                if auth.get("issuer_url") and auth["issuer_url"] not in servers:
                    raise ValueError("Configured OAuth issuer is not advertised by this resource")
                issuer = auth.get("issuer_url") or servers[0]
            except (ValueError, TypeError) as exc:
                raise GatewayError("oauth_metadata_invalid", str(exc)) from None
        issuer_parts = checked_url(issuer, resource)
        if issuer_parts.query:
            raise GatewayError("oauth_metadata_invalid", "OAuth issuer cannot contain a query")
        issuer_origin = urlunsplit((issuer_parts.scheme, issuer_parts.netloc, "", "", ""))
        path = issuer_parts.path.rstrip("/")
        urls = list(dict.fromkeys([
            issuer_origin + "/.well-known/oauth-authorization-server" + path,
            issuer_origin + "/.well-known/openid-configuration" + path,
            issuer_origin + path + "/.well-known/openid-configuration",
        ]))
        metadata = None
        for url in urls:
            try:
                metadata = await json_request(client, url, resource)
                break
            except Unavailable:
                continue
        if metadata is None:
            if all(auth.get(key) for key in ("authorization_url", "token_url")):
                result = {key: auth[key] for key in ("authorization_url", "token_url", "registration_url", "issuer_url")
                          if auth.get(key)}
                for key in ("authorization_url", "token_url", "registration_url"):
                    if result.get(key):
                        checked_url(result[key], resource)
                return result | {"issuer_url": issuer, "resource": resource, "metadata_discovered": True,
                                 "cimd_supported": False, "scopes": auth.get("scopes", [])}
            raise GatewayError("oauth_discovery_failed", "无法发现 OAuth 端点，请填写授权/Token URL 或检查服务器元数据")
        if metadata.get("issuer") != issuer:
            raise GatewayError("oauth_metadata_invalid", "OAuth metadata issuer does not match the expected issuer")
        for key in ("client_id_metadata_document_supported", "authorization_response_iss_parameter_supported"):
            if key in metadata and type(metadata[key]) is not bool:
                raise GatewayError("oauth_metadata_invalid", "OAuth metadata capability must be a boolean")
        for key in ("code_challenge_methods_supported", "token_endpoint_auth_methods_supported"):
            if key in metadata and (not isinstance(metadata[key], list) or not all(isinstance(x, str) for x in metadata[key])):
                raise GatewayError("oauth_metadata_invalid", "OAuth metadata methods must be a string array")
        if "code_challenge_methods_supported" in metadata and "S256" not in metadata["code_challenge_methods_supported"]:
            raise GatewayError("oauth_metadata_invalid", "OAuth server does not support required PKCE S256")
        result = {"issuer_url": issuer, "resource": resource, "metadata_discovered": True,
                  "cimd_supported": metadata.get("client_id_metadata_document_supported", False),
                  "iss_required": metadata.get("authorization_response_iss_parameter_supported", False)}
        for source, destination in (("authorization_endpoint", "authorization_url"), ("token_endpoint", "token_url"),
                                    ("registration_endpoint", "registration_url")):
            value = metadata.get(source)
            if value:
                checked_url(value, resource)
                result[destination] = value
            elif source != "registration_endpoint":
                raise GatewayError("oauth_metadata_invalid", "OAuth metadata is missing " + source)
        scopes = auth.get("scopes") or challenge.get("scope") or (protected or {}).get("scopes_supported", [])
        if not isinstance(scopes, (str, list)) or isinstance(scopes, list) and not all(isinstance(x, str) for x in scopes):
            raise GatewayError("oauth_metadata_invalid", "OAuth scopes are invalid")
        result["scopes"] = scopes.split() if isinstance(scopes, str) else scopes
        if isinstance(metadata.get("scopes_supported"), list):
            result["scopes_supported"] = metadata["scopes_supported"]
        if "token_endpoint_auth_methods_supported" in metadata:
            result["token_endpoint_auth_methods_supported"] = metadata["token_endpoint_auth_methods_supported"]
        return result


def auth_method(auth, metadata):
    method = auth.get("token_endpoint_auth_method")
    supported = metadata.get("token_endpoint_auth_methods_supported")
    if method is None:
        method = ("client_secret_basic" if supported and "client_secret_basic" in supported else "client_secret_post") if auth.get("client_secret") else "none"
    if not isinstance(method, str) or method not in {"none", "client_secret_basic", "client_secret_post"}:
        raise GatewayError("oauth_configuration_required", "Unsupported token authentication method")
    if supported is not None and method not in supported:
        raise GatewayError("oauth_configuration_required", "OAuth server does not support the configured token authentication method")
    if method != "none":
        import time

        secret = auth.get("client_secret")
        expiry = auth.get("client_secret_expires_at", 0)
        if not isinstance(secret, str) or not secret.strip():
            raise GatewayError("oauth_configuration_required", "注册未返回有效的 Client Secret，无法使用所选 Token 认证方式；请重新授权或使用预注册")
        if type(expiry) is not int or expiry < 0 or expiry and expiry <= time.time():
            raise GatewayError("oauth_configuration_required", "注册的 Client Secret 已过期或有效期无效，请重新授权")
    elif auth.get("client_secret"):
        raise GatewayError("oauth_configuration_required", "none authentication cannot send a Client Secret")
    return method


def registration_method(auth, metadata):
    """Choose the requested DCR method before a generated secret exists."""
    supported = metadata.get("token_endpoint_auth_methods_supported")
    chosen = auth.get("token_endpoint_auth_method")
    if chosen is None:
        chosen = next((method for method in ("none", "client_secret_post", "client_secret_basic")
                       if supported is None or method in supported), None)
    if not isinstance(chosen, str) or chosen not in {"none", "client_secret_basic", "client_secret_post"} or supported is not None and chosen not in supported:
        raise GatewayError("oauth_configuration_required", "OAuth server does not support the configured token authentication method")
    return chosen


def figma_registration(context):
    """Narrow compatibility for Figma's contradictory public registration response."""
    return (context.get("issuer_url") == "https://api.figma.com"
            and context.get("resource") == "https://mcp.figma.com/mcp"
            and context.get("registration_url") == "https://api.figma.com/v1/oauth/mcp/register"
            and context.get("token_url") == "https://api.figma.com/v1/oauth/token")


async def register(context, client_name, redirect_uri):
    from .oauth_config import local_host

    requested = context.get("token_endpoint_auth_method", "none")
    body = {"client_name": client_name, "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": requested,
            "application_type": "native" if local_host(urlsplit(redirect_uri).hostname or "") else "web"}
    async with http_client() as client:
        value = await json_request(client, context["registration_url"], context["resource"], data=body)
    identity = value.get("client_id")
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 4096:
        raise GatewayError("oauth_registration_failed", "DCR response contains no valid Client ID")
    method = value.get("token_endpoint_auth_method", requested)
    if not isinstance(method, str) or method not in {"none", "client_secret_basic", "client_secret_post"}:
        raise GatewayError("oauth_registration_failed", "DCR response contains an unsupported token authentication method")
    if (method == "none" and requested != "none" and value.get("client_secret")
            and figma_registration(context)):
        method = requested
    if context.get("token_auth_method_explicit") and method != requested:
        raise GatewayError("oauth_registration_failed", "服务器返回的 Token 认证方式与所选方式不一致，请调整配置后重新授权")
    result = {"client_id": identity, "token_endpoint_auth_method": method}
    if method != "none":
        result["client_secret"] = value.get("client_secret")
        if "client_secret_expires_at" in value:
            result["client_secret_expires_at"] = value["client_secret_expires_at"]
    try:
        auth_method(result, context)
    except GatewayError as exc:
        raise GatewayError("oauth_registration_failed", str(exc)) from None
    return result
