"""Static CIMD documents, public-only verification and server-owned evidence."""
import asyncio
import hashlib
import ipaddress
import json
import math
import time
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .database import SystemSetting
from .identity import current_user, revalidate_user
from .oauth_config import CALLBACK_PATHS, callback_uri, client_metadata, oauth_url
from .oauth_http import OAuthHTTPTransport
from .runtime import GatewayError

router = APIRouter(prefix="/api/v1/oauth/client-metadata")
USER = Depends(current_user)
VERIFICATION_TTL = 86400
MAX_BYTES = 65536
MAX_RECORDS = 32


class MetadataError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def metadata_url(url):
    if not isinstance(url, str) or len(url) > 4096:
        raise MetadataError("invalid_url", "元数据 URL 格式不正确或过长")
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise MetadataError("not_https", "客户端元数据 URL 必须使用 HTTPS")
        if not parsed.path:
            raise MetadataError("no_path", "元数据 URL 必须包含路径，例如 /mmg.json")
        oauth_url(url)
        if "#" in url or any(part in {".", ".."} for part in unquote(parsed.path).split("/")):
            raise ValueError()
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise MetadataError("unsafe_address", "元数据必须托管在公网 HTTPS 地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise MetadataError("unsafe_address", "元数据地址不能指向回环、内网或链路本地网络")
        return parsed
    except MetadataError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise MetadataError("invalid_url", "元数据 URL 不能包含凭据、fragment、点路径或非法字符") from None


def expected_callback(public_url, path):
    if path not in CALLBACK_PATHS:
        raise MetadataError("invalid_callback", "请选择受支持的 OAuth 回调路径")
    try:
        parsed = oauth_url(public_url)
        if ("?" in public_url or "#" in public_url or parsed.hostname in {"0.0.0.0", "::"}
                or any(part in {".", ".."} for part in unquote(parsed.path).split("/"))):
            raise ValueError()
    except (ValueError, TypeError):
        raise MetadataError("invalid_public_url",
            "PUBLIC_URL 必须是浏览器可访问的 HTTPS 地址，或同机 HTTP 回环地址；不能使用 0.0.0.0") from None
    return callback_uri(public_url, {"callback_path": path})


def generate_client_metadata(public_url, hosting_url, path, client_name="Codex"):
    metadata_url(hosting_url)
    redirect_uri = expected_callback(public_url, path)
    return {"client_id": hosting_url, "client_name": client_name.strip() or "Codex",
            "redirect_uris": [redirect_uri],
            "application_type": "native" if urlsplit(redirect_uri).scheme == "http" else "web",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"}


def document_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_cimd_metadata(value, url, redirect_uri):
    metadata_url(url)
    if not isinstance(value, dict):
        raise MetadataError("invalid_metadata", "元数据必须是 JSON 对象")
    # Never accept shared secrets, tokens or private key material in a public document.
    forbidden = {"client_secret", "client_secret_expires_at", "access_token", "refresh_token",
                 "id_token", "registration_access_token", "private_key",
                 "d", "p", "q", "dp", "dq", "qi", "oth", "k"}
    def inspect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in forbidden:
                    raise MetadataError("sensitive_metadata", "公开元数据不能包含 Secret、Token 或私钥")
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
        elif isinstance(item, float) and not math.isfinite(item):
            raise MetadataError("invalid_metadata", "元数据数值必须为有限 JSON 数字")
    inspect(value)
    if value.get("client_id") != url:
        raise MetadataError("client_id_mismatch", "文档内 client_id 必须与托管 URL 逐字符一致")
    if not isinstance(value.get("client_name"), str) or not value["client_name"].strip():
        raise MetadataError("invalid_metadata", "元数据必须包含非空 client_name")
    redirects = value.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or len(redirects) > 100:
        raise MetadataError("invalid_redirect_uris", "redirect_uris 必须是非空回调地址数组")
    try:
        for uri in redirects:
            oauth_url(uri)
    except (TypeError, ValueError):
        raise MetadataError("invalid_redirect_uris", "回调必须为合法 HTTPS URL；HTTP 仅允许回环地址") from None
    if redirect_uri not in redirects:
        raise MetadataError("redirect_uri_mismatch",
            "文档未包含 MMG 实际使用的完整回调地址；主机、端口、协议和路径必须一致")
    if value.get("token_endpoint_auth_method", "none") != "none":
        raise MetadataError("unsupported_auth_method", "当前 MMG 的 CIMD 仅支持 none + PKCE")
    for key, required in (("grant_types", "authorization_code"), ("response_types", "code")):
        if key in value and (not isinstance(value[key], list) or required not in value[key]
                             or not all(isinstance(x, str) for x in value[key])):
            raise MetadataError("invalid_metadata", key + " 与授权码流程不兼容")
    for key in ("client_uri", "logo_uri"):
        if key in value:
            try:
                oauth_url(value[key])
            except (TypeError, ValueError):
                raise MetadataError("invalid_metadata", key + " 必须为合法 HTTP(S) URL") from None
    return value


def http_client():
    return httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False,
                             transport=OAuthHTTPTransport())


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


async def fetch_metadata(url):
    metadata_url(url)
    try:
        async with (
            asyncio.timeout(5),
            http_client() as client,
            client.stream("GET", url, headers={"Accept": "application/json"},
                          extensions={"oauth_public_only": True}) as response,
        ):
            if 300 <= response.status_code < 400:
                raise MetadataError("redirect_not_allowed", "托管 URL 必须直接返回 JSON，不能通过 HTTP 跳转")
            if response.status_code != 200:
                raise MetadataError("http_error", "托管地址未返回 HTTP 200，请检查部署和访问权限")
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise MetadataError("response_too_large", "元数据响应超过 64 KiB")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    except GatewayError:
        raise MetadataError("unsafe_address", "元数据域名解析到了非公网地址，已阻止访问") from None
    except (httpx.HTTPError, TimeoutError):
        raise MetadataError("unreachable", "托管地址无法访问，请检查部署、TLS 证书与 MMG 出站网络") from None
    try:
        value = json.loads(raw, object_pairs_hook=_json_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise MetadataError("not_json", "返回内容不是合规 JSON，请使用直接提供 JSON 的静态托管地址") from None
    if content_type != "application/json":
        raise MetadataError("invalid_content_type", "请将 Content-Type 设置为 application/json；GitHub 可使用 Pages")
    return value


class CIMDVerifier:
    """Bounded per-user evidence is separate from editable OAuth configuration."""
    def __init__(self, catalog):
        self.catalog = catalog

    def setting_key(self, user_id):
        return "oauth-cimd:" + str(user_id)

    def binding(self, url, redirect):
        return document_hash([url, redirect])

    async def status(self, user_id, url, redirect, draft=None):
        async with self.catalog.db.locked() as session:
            row = await session.get(SystemSetting, self.setting_key(user_id))
            records = self.catalog.unseal(row.value) if row else {}
            record = records.get(self.binding(url, redirect), {})
        if not record:
            return {"ok": False, "status": "unverified"}
        result = dict(record)
        if result.get("ok") and (time.time() - result["verified_at"] >= VERIFICATION_TTL
                                or result["verified_at"] > time.time()
                                or draft is not None and document_hash(draft) != result.get("document_hash")):
            result.update(ok=False, status="stale")
        return result

    async def verify(self, user_id, url, redirect, draft=None):
        try:
            if draft is not None:
                validate_cimd_metadata(draft, url, redirect)
            value = validate_cimd_metadata(await fetch_metadata(url), url, redirect)
            if draft is not None and document_hash(draft) != document_hash(value):
                raise MetadataError("document_mismatch", "线上文件与当前编辑内容不同，请先发布当前 JSON 再验证")
            result = {"ok": True, "status": "verified", "verified_at": time.time(),
                      "document_hash": document_hash(value), "code": None, "message": "已验证线上元数据"}
        except (MetadataError, RecursionError) as exc:
            result = {"ok": False, "status": "failed", "verified_at": None,
                      "code": getattr(exc, "code", "invalid_metadata"),
                      "message": str(exc) if isinstance(exc, MetadataError) else "元数据嵌套层级过深"}
        result["checked_at"] = time.time()
        async with self.catalog.db.locked() as session:
            key = self.setting_key(user_id)
            row = await session.get(SystemSetting, key)
            records = self.catalog.unseal(row.value) if row else {}
            records[self.binding(url, redirect)] = result
            records = dict(sorted(records.items(), key=lambda pair: pair[1]["checked_at"])[-MAX_RECORDS:])
            if row:
                row.value = self.catalog.seal(records)
            else:
                session.add(SystemSetting(key=key, value=self.catalog.seal(records)))
        await self.catalog.logs.audit("oauth.cimd_verify", user_id,
                                      {"result": result["status"], "code": result["code"],
                                       "binding": self.binding(url, redirect)})
        return result

    async def ensure(self, request, user_id, auth):
        url = auth.get("cimd_external_url", "")
        path = auth.get("callback_path", CALLBACK_PATHS[0])
        try:
            metadata_url(url)
            redirect = expected_callback(self.catalog.config.public_url, path)
            draft = auth.get("cimd_metadata")
            result = await self.status(user_id, url, redirect, draft)
            if not result["ok"]:
                await verification_rate(request, user_id)
                result = await self.verify(user_id, url, redirect, draft)
            if not result["ok"]:
                raise MetadataError(result["code"], result["message"])
        except MetadataError as exc:
            raise GatewayError("oauth_configuration_required", "CIMD [" + exc.code + "] " + str(exc)) from None
        return url


async def verification_rate(request, user_id):
    try:
        await request.app.state.identity_rates.check(("cimd-verify", user_id), 5, 60, consume=True)
    except HTTPException as exc:
        if exc.status_code != 429:
            raise
        raise HTTPException(429, "线上验证过于频繁，每用户每分钟最多 5 次，请稍后重试", headers=exc.headers) from None


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = "external"
    url: str = Field(max_length=4096)
    callback_path: str = CALLBACK_PATHS[0]
    metadata: dict | None = None


@router.get("/context")
async def metadata_context(request: Request, callback_path: str = CALLBACK_PATHS[0], user=USER):
    public_url = request.app.state.config.public_url
    try:
        redirect = expected_callback(public_url, callback_path)
        return {"public_url": public_url, "redirect_uri": redirect}
    except MetadataError as exc:
        return {"public_url": public_url, "redirect_uri": None, "code": exc.code, "message": str(exc)}


@router.get("/template")
async def template(request: Request, source: str = "mmg", hosting_url: str = "",
                   callback_path: str = CALLBACK_PATHS[0], client_name: str = "Codex", user=USER):
    if source == "mmg":
        return client_metadata(request.app.state.config.public_url)
    if source != "external":
        raise ValueError("Unknown CIMD metadata source")
    return generate_client_metadata(request.app.state.config.public_url, hosting_url, callback_path, client_name)


@router.post("/verify")
@router.post("/status")
async def verify_document(data: VerificationInput, request: Request, user=USER):
    async with request.app.state.db.locked() as session:
        user = await revalidate_user(request, session)
    is_verification = request.url.path.endswith("/verify")
    if is_verification:
        await verification_rate(request, user.id)
    try:
        if data.source != "external":
            raise MetadataError("invalid_source", "只对外部静态文件执行验证")
        metadata_url(data.url)
        redirect = expected_callback(request.app.state.config.public_url, data.callback_path)
        if data.metadata is not None:
            if len(json.dumps(data.metadata, ensure_ascii=False).encode()) > MAX_BYTES:
                raise MetadataError("response_too_large", "编辑的元数据超过 64 KiB")
            validate_cimd_metadata(data.metadata, data.url, redirect)
    except MetadataError as exc:
        if is_verification:
            await request.app.state.catalog.logs.audit("oauth.cimd_verify", user.id,
                {"result": "failed", "code": exc.code,
                 "binding": document_hash([data.url, data.callback_path])})
        return {"ok": False, "status": "failed", "code": exc.code, "message": str(exc), "verified_at": None}
    verifier = request.app.state.oauth.cimd
    if request.url.path.endswith("/status"):
        return await verifier.status(user.id, data.url, redirect, data.metadata)
    return await verifier.verify(user.id, data.url, redirect, data.metadata)
