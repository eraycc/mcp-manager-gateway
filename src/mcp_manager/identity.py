"""Cookie sessions, opaque API tokens and permission intersections."""
import hashlib
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from .database import ApiToken, AuthSession, Database, McpServer, SystemSetting, User, get_setting, now, uid
from .token_secrets import decrypt_token_secret, encrypt_token_secret

router = APIRouter(prefix="/api/v1")
hasher = PasswordHasher()
ISSUER = "mcp-manager"
AUDIENCE = "mcp-manager-console"


def expired(value):
    return value is not None and (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)) <= now()


def public(obj):
    result = {c.name: getattr(obj, c.name) for c in obj.__table__.columns
              if c.name not in {"password_hash", "token_hash", "token_secret", "auth_version"}}
    result = {key: value.replace(tzinfo=UTC) if isinstance(value, datetime) and value.tzinfo is None else value
              for key, value in result.items()}
    if isinstance(obj, ApiToken):
        result["secret_available"] = bool(obj.token_secret)
    return result


def origin_check(request):
    origin = request.headers.get("origin")
    if not origin:
        return

    def origin_key(value):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment):
                return None
            return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None

    # The console can be reached through localhost, LAN IPs or deployment domains.
    # PUBLIC_URL remains an optional external origin for reverse-proxy deployments.
    actual = str(request.url.replace(path="", query="", fragment=""))
    allowed = {origin_key(actual), origin_key(request.app.state.config.public_url)} - {None}
    if origin_key(origin) not in allowed:
        raise HTTPException(403, "Origin is not allowed")


async def revalidate_user(request: Request, session, *, require_admin: bool = False) -> User:
    """Reload the cookie actor in an existing transaction.

    Call immediately after acquiring db.locked(), before any mutation or
    privileged dispatch. Keep that lock through the authorized write, and use
    the returned actor for role/ownership checks. Do not nest db.locked() or
    set_setting() inside that transaction. This helper never commits.
    """
    try:
        payload = jwt.decode(request.cookies.get("mcp_session", ""), request.app.state.config.jwt_secret,
                             algorithms=["HS256"], issuer=ISSUER, audience=AUDIENCE,
                             options={"require": ["sub", "jti", "iss", "aud"]})
    except jwt.PyJWTError:
        raise HTTPException(401, "Login required")
    auth = await session.get(AuthSession, payload["jti"], populate_existing=True)
    user = await session.get(User, payload["sub"], populate_existing=True)
    if (not user or user.disabled or not auth or auth.revoked
            or auth.user_id != user.id or auth.auth_version != user.auth_version
            or expired(auth.expires_at)):
        raise HTTPException(401, "Session revoked or expired")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin_check(request)
        cookie, header = request.cookies.get("mcp_csrf"), request.headers.get("X-CSRF-Token")
        if not cookie or not header or not secrets.compare_digest(cookie, header):
            raise HTTPException(403, "CSRF token missing or invalid")
    if require_admin and user.role != "admin":
        raise HTTPException(403, "Administrator required")
    request.state.session_id = auth.id
    return user


async def current_user(request: Request) -> User:
    async with request.app.state.db.session() as session:
        return await revalidate_user(request, session)


async def admin_user(request: Request) -> User:
    user = await current_user(request)
    if user.role != "admin":
        raise HTTPException(403, "Administrator required")
    return user


async def effective_mcp_ids(db: Database, user: User, token: ApiToken | None = None) -> set[str]:
    if user.disabled:
        return set()
    statement = select(McpServer.id).where(McpServer.mode != "disabled")
    if user.role != "admin" and user.scope_mode != "all":
        statement = statement.where(McpServer.id.in_(user.mcp_ids))
    if token and token.scope_mode != "all":
        statement = statement.where(McpServer.id.in_(token.mcp_ids))
    async with db.session() as s:
        return set((await s.scalars(statement)).all())


async def authenticate_token(request: Request):
    db = request.app.state.db
    header = request.headers.get("Authorization")
    if not header:
        if await get_setting(db, "token_auth_enabled", True):
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        async with db.session() as s:
            ids = set((await s.scalars(select(McpServer.id).where(McpServer.mode != "disabled"))).all())
        if await get_setting(db, "anonymous_scope_mode", "selected") != "all":
            ids &= set(await get_setting(db, "anonymous_mcp_ids", []))
        return None, None, ids
    scheme, _, secret = header.partition(" ")
    if scheme.lower() != "bearer" or not secret:
        raise HTTPException(401, "Invalid bearer token")
    digest = hashlib.sha256(secret.encode()).hexdigest()
    async with db.session() as s:
        token = await s.scalar(select(ApiToken).where(ApiToken.token_hash == digest))
        user = await s.get(User, token.user_id) if token else None
        if not token or token.disabled or expired(token.expires_at) or not user or user.disabled:
            raise HTTPException(401, "Token revoked or expired")
    return user, token, await effective_mcp_ids(db, user, token)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("expires_at", check_fields=False)
    @classmethod
    def expiry_utc(cls, value):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Register(Input):
    username: str = Field(min_length=3, max_length=64, pattern=r"^\S+$")
    email: str = Field(default="", max_length=254)
    password: str = Field(min_length=10, max_length=1024)


class Login(Input):
    username: str
    password: str


class UserCreate(Register):
    role: Literal["admin", "user"] = "user"
    disabled: bool = False
    scope_mode: Literal["selected", "all"] = "selected"
    mcp_ids: list[str] = Field(default_factory=list)


class ProfilePatch(Input):
    username: str | None = Field(default=None, min_length=3, max_length=64, pattern=r"^\S+$")
    email: str | None = Field(default=None, max_length=254)
    current_password: str | None = None
    password: str | None = Field(default=None, min_length=10, max_length=1024)


class UserPatch(ProfilePatch):
    role: Literal["admin", "user"] | None = None
    disabled: bool | None = None
    scope_mode: Literal["selected", "all"] | None = None
    mcp_ids: list[str] | None = None


class Batch(Input):
    action: Literal["disable", "enable", "delete"]
    ids: list[str] = Field(min_length=1, max_length=500)


class TokenCreate(Input):
    name: str = Field(min_length=1, max_length=128)
    user_id: str | None = None
    scope_mode: Literal["selected", "all"] = "selected"
    mcp_ids: list[str] = Field(default_factory=list)
    discovery_mode: Literal["native", "discovery"] = "discovery"
    enable_resource_tools: bool = False
    enable_mcp_proposals: bool = False
    expires_at: datetime | None = None


class TokenPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    scope_mode: Literal["selected", "all"] | None = None
    mcp_ids: list[str] | None = None
    discovery_mode: Literal["native", "discovery"] | None = None
    enable_resource_tools: bool | None = None
    enable_mcp_proposals: bool | None = None
    expires_at: datetime | None = None
    disabled: bool | None = None


def verify_password(value, encoded):
    try:
        return hasher.verify(encoded, value)
    except VerificationError:
        return False


async def commit_user(s):
    try:
        await s.flush()
    except IntegrityError:
        raise HTTPException(409, "Username already exists")


async def last_admin(s):
    count = await s.scalar(select(func.count()).select_from(User).where(
        User.role == "admin", User.disabled.is_(False)))
    if not count:
        raise HTTPException(409, "At least one enabled administrator is required")


async def remove_user(s, user):
    for prefix in ("oauth:", "credential:"):
        await s.execute(delete(SystemSetting).where(
            SystemSetting.key.startswith(prefix, autoescape=True),
            SystemSetting.key.endswith(":" + user.id, autoescape=True),
        ))
    await s.execute(delete(ApiToken).where(ApiToken.user_id == user.id))
    await s.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    await s.delete(user)


@router.get("/bootstrap")
async def bootstrap(request: Request):
    db = request.app.state.db
    async with db.session() as s:
        initialized = bool(await s.scalar(select(func.count()).select_from(User)))
    return {"initialized": initialized,
            "registration_enabled": await get_setting(db, "registration_enabled", True),
            "title": await get_setting(db, "title", "MCP Manager")}


@router.post("/auth/register")
async def register(data: Register, request: Request):
    origin_check(request)
    db = request.app.state.db
    encoded = hasher.hash(data.password)
    async with db.locked() as s:
        setting = await s.get(SystemSetting, "registration_enabled")
        if setting and not setting.value:
            raise HTTPException(403, "Registration is disabled")
        count = await s.scalar(select(func.count()).select_from(User))
        user = User(username=data.username, email=data.email, password_hash=encoded,
                    role="admin" if count == 0 else "user")
        s.add(user)
        await commit_user(s)
        result = public(user)
    return result


@router.post("/auth/login")
async def login(data: Login, request: Request, response: Response):
    origin_check(request)
    db, config = request.app.state.db, request.app.state.config
    days = await get_setting(db, "jwt_days", 30)
    async with db.locked() as s:
        user = await s.scalar(select(User).where(User.username == data.username))
        if not user or user.disabled or not verify_password(data.password, user.password_hash):
            raise HTTPException(401, "Invalid username or password")
        expiry = now() + timedelta(days=float(days)) if days else None
        session = AuthSession(
            id=uid(), user_id=user.id, auth_version=user.auth_version, expires_at=expiry,
            ip_address=request.client.host[:64] if request.client else None,
            user_agent=(request.headers.get("user-agent") or "")[:1024] or None,
        )
        s.add(session)
        payload = {"sub": user.id, "jti": session.id, "iss": ISSUER, "aud": AUDIENCE, "iat": now()}
        if expiry:
            payload["exp"] = expiry
        encoded = jwt.encode(payload, config.jwt_secret, algorithm="HS256")
        csrf = secrets.token_urlsafe(32)
        max_age = int(float(days) * 86400) if days else 10 * 365 * 86400
        for name, value, http_only in (("mcp_session", encoded, True), ("mcp_csrf", csrf, False)):
            response.set_cookie(name, value, httponly=http_only, secure=config.cookie_secure,
                                samesite="lax", max_age=max_age, path="/")
        return public(user)


CURRENT_USER = Depends(current_user)
ADMIN_USER = Depends(admin_user)


@router.post("/auth/logout")
async def logout(request: Request, response: Response, user=CURRENT_USER):
    async with request.app.state.db.locked() as s:
        await revalidate_user(request, s)
        session = await s.get(AuthSession, request.state.session_id)
        session.revoked = True
    response.delete_cookie("mcp_session")
    response.delete_cookie("mcp_csrf")
    return {"ok": True}


@router.get("/me")
async def me(user=CURRENT_USER):
    return public(user)


@router.patch("/me")
async def update_me(data: ProfilePatch, request: Request, user=CURRENT_USER):
    async with request.app.state.db.locked() as s:
        user = await revalidate_user(request, s)
        target = await s.get(User, user.id)
        values = data.model_dump(exclude_unset=True, exclude_none=True)
        if data.password:
            if not data.current_password or not verify_password(data.current_password, target.password_hash):
                raise HTTPException(400, "Current password is incorrect")
            target.password_hash = hasher.hash(data.password)
            target.auth_version += 1
            own = await s.get(AuthSession, request.state.session_id)
            own.auth_version = target.auth_version
        for key in ("username", "email"):
            if key in values:
                setattr(target, key, values[key])
        await commit_user(s)
        return public(target)


@router.get("/me/sessions")
async def sessions(request: Request, user=CURRENT_USER):
    async with request.app.state.db.session() as s:
        rows = (await s.scalars(select(AuthSession).where(AuthSession.user_id == user.id))).all()
        return [dict(public(row), current=row.id == request.state.session_id,
                     active=not row.revoked and not expired(row.expires_at)
                     and row.auth_version == user.auth_version) for row in rows]


@router.delete("/me/sessions/{session_id}")
async def revoke_session(session_id: str, request: Request, user=CURRENT_USER):
    async with request.app.state.db.locked() as s:
        user = await revalidate_user(request, s)
        item = await s.get(AuthSession, session_id)
        if not item or item.user_id != user.id:
            raise HTTPException(404, "Session not found")
        item.revoked = True
    return {"ok": True}


@router.delete("/me/sessions/{session_id}/permanent")
async def delete_session(session_id: str, request: Request, user=CURRENT_USER):
    async with request.app.state.db.locked() as s:
        user = await revalidate_user(request, s)
        item = await s.get(AuthSession, session_id)
        if not item or item.user_id != user.id:
            raise HTTPException(404, "Session not found")
        if item.id == request.state.session_id:
            raise HTTPException(409, "Cannot delete the current session")
        await s.delete(item)
    return {"ok": True}


def page_result(items, total, page, page_size):
    return {"items": [public(x) for x in items], "total": total, "page": page,
            "page_size": page_size, "total_pages": math.ceil(total / page_size)}


async def page_query(db, statement, model, page, page_size):
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, "page >= 1 and page_size between 1 and 200 required")
    async with db.session() as s:
        total = await s.scalar(select(func.count()).select_from(statement.subquery()))
        items = (await s.scalars(statement.order_by(model.created_at.desc(), model.id)
                                 .offset((page - 1) * page_size).limit(page_size))).all()
        return page_result(items, total, page, page_size)


@router.get("/users")
async def users(request: Request, q: str = "", page: int = 1, page_size: int = 20,
                role: str | None = None, disabled: bool | None = None, user=ADMIN_USER):
    statement = select(User)
    if q:
        statement = statement.where(or_(User.username.contains(q, autoescape=True),
                                       User.email.contains(q, autoescape=True)))
    if role:
        statement = statement.where(User.role == role)
    if disabled is not None:
        statement = statement.where(User.disabled == disabled)
    return await page_query(request.app.state.db, statement, User, page, page_size)


@router.post("/users")
async def create_user(data: UserCreate, request: Request, user=ADMIN_USER):
    values = data.model_dump(exclude={"password"})
    async with request.app.state.db.locked() as s:
        await revalidate_user(request, s, require_admin=True)
        target = User(**values, password_hash=hasher.hash(data.password))
        s.add(target)
        await commit_user(s)
        return public(target)


@router.post("/users/batch")
async def batch_users(data: Batch, request: Request, user=ADMIN_USER):
    async with request.app.state.db.locked() as s:
        await revalidate_user(request, s, require_admin=True)
        targets = (await s.scalars(select(User).where(User.id.in_(data.ids)))).all()
        for target in targets:
            if data.action == "delete":
                await remove_user(s, target)
            else:
                target.disabled = data.action == "disable"
                if target.disabled:
                    target.auth_version += 1
        await s.flush()
        await last_admin(s)
    return {"ok": True, "count": len(targets)}


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, data: UserPatch, request: Request, user=ADMIN_USER):
    async with request.app.state.db.locked() as s:
        await revalidate_user(request, s, require_admin=True)
        target = await s.get(User, user_id)
        if not target:
            raise HTTPException(404, "User not found")
        values = data.model_dump(exclude_unset=True, exclude_none=True, exclude={"current_password", "password"})
        for key, value in values.items():
            setattr(target, key, value)
        if data.password:
            target.password_hash = hasher.hash(data.password)
        if data.password or "disabled" in values or "role" in values:
            target.auth_version += 1
        await commit_user(s)
        await last_admin(s)
        return public(target)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request, user=ADMIN_USER):
    async with request.app.state.db.locked() as s:
        await revalidate_user(request, s, require_admin=True)
        target = await s.get(User, user_id)
        if not target:
            raise HTTPException(404, "User not found")
        await remove_user(s, target)
        await s.flush()
        await last_admin(s)
    return {"ok": True}


async def scope_check(db, owner, mode, ids):
    if mode == "selected" and not set(ids) <= await effective_mcp_ids(db, owner):
        raise HTTPException(403, "Token scope exceeds owner permissions")


def mint_token():
    secret = "mcpm_" + secrets.token_urlsafe(36)
    return secret, hashlib.sha256(secret.encode()).hexdigest(), secret[:16]


async def owned_token(s, token_id, user):
    token = await s.get(ApiToken, token_id)
    if not token or (user.role != "admin" and token.user_id != user.id):
        raise HTTPException(404, "Token not found")
    return token


@router.get("/tokens")
async def tokens(request: Request, q: str = "", page: int = 1, page_size: int = 20,
                 disabled: bool | None = None, view: Literal["mine", "all"] = "mine",
                 username: str = "", user_id: str | None = None, user=CURRENT_USER):
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, "page >= 1 and page_size between 1 and 200 required")
    statement = select(ApiToken, User.username).join(User, User.id == ApiToken.user_id)
    if view == "all":
        if user.role != "admin":
            raise HTTPException(403, "Administrator required for all token owners")
        if user_id:
            statement = statement.where(ApiToken.user_id == user_id)
        if username:
            statement = statement.where(func.lower(User.username).contains(username.lower(), autoescape=True))
    else:
        if username or user_id:
            raise HTTPException(422, "Owner filters require view=all")
        statement = statement.where(ApiToken.user_id == user.id)
    if q:
        statement = statement.where(ApiToken.name.contains(q, autoescape=True))
    if disabled is not None:
        statement = statement.where(ApiToken.disabled == disabled)
    db = request.app.state.db
    async with db.session() as session:
        total = await session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = (await session.execute(
            statement.order_by(ApiToken.created_at.desc(), ApiToken.id)
            .offset((page - 1) * page_size).limit(page_size)
        )).all()
    result = {
        "items": [public(token) | {"username": owner_name} for token, owner_name in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size),
    }
    logs = getattr(request.app.state, "logs", None)
    counts = await logs.token_counts([x["id"] for x in result["items"]]) if logs else {}
    for item in result["items"]:
        item.update(counts.get(item["id"], {"call_count": 0, "success_count": 0, "failed_count": 0}))
    return result


@router.post("/tokens")
async def create_token(data: TokenCreate, request: Request, response: Response, user=CURRENT_USER):
    db = request.app.state.db
    async with db.locked() as s:
        user = await revalidate_user(request, s)
        owner_id = data.user_id or user.id
        if owner_id != user.id and user.role != "admin":
            raise HTTPException(403, "Cannot create tokens for another user")
        owner = await s.get(User, owner_id)
        if not owner or owner.disabled:
            raise HTTPException(400, "Token owner is unavailable")
        if data.enable_mcp_proposals and (user.role != "admin" or owner.role != "admin"):
            raise HTTPException(403, "Only administrators may create proposal-enabled tokens")
        await scope_check(db, owner, data.scope_mode, data.mcp_ids)
        secret, digest, prefix = mint_token()
        token = ApiToken(**data.model_dump(exclude={"user_id"}), user_id=owner_id,
                         token_hash=digest, token_secret=encrypt_token_secret(request.app.state.config, secret),
                         prefix=prefix)
        s.add(token)
        await s.flush()
        response.headers["Cache-Control"] = "no-store"
        return dict(public(token), token=secret)


async def revoke_token_leases(request, token_id):
    state = request.app.state
    if hasattr(state, "runtime"):
        await state.runtime.revoke(token_id=token_id)
    if hasattr(state, "gateway"):
        await state.gateway.revoke(token_id=token_id)


@router.get("/tokens/{token_id}/secret")
async def reveal_token_secret(token_id: str, request: Request, response: Response, user=CURRENT_USER):
    async with request.app.state.db.locked() as session:
        user = await revalidate_user(request, session)
        token = await owned_token(session, token_id, user)
        secret = decrypt_token_secret(request.app.state.config, token.token_secret) if token.token_secret else None
        owner_id = token.user_id
    if secret is None:
        return JSONResponse(
            {
                "detail": "This legacy token secret cannot be recovered; rotate it once.",
                "code": "token_secret_unavailable",
            },
            status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    logs = getattr(request.app.state, "logs", None)
    if logs:
        await logs.audit("token.secret.reveal", user.id, {"token_id": token_id, "owner_id": owner_id})
    response.headers["Cache-Control"] = "no-store"
    return {"token": secret}


@router.patch("/tokens/{token_id}")
async def patch_token(token_id: str, data: TokenPatch, request: Request, user=CURRENT_USER):
    db = request.app.state.db
    async with db.locked() as s:
        user = await revalidate_user(request, s)
        token = await owned_token(s, token_id, user)
        owner = await s.get(User, token.user_id)
        values = data.model_dump(exclude_unset=True)
        if values.get("enable_mcp_proposals") and (user.role != "admin" or owner.role != "admin"):
            raise HTTPException(403, "Only administrators may enable MCP proposals")
        scope_changed = ((data.scope_mode is not None and data.scope_mode != token.scope_mode)
                         or (data.mcp_ids is not None and set(data.mcp_ids) != set(token.mcp_ids)))
        for key, value in values.items():
            if value is not None or key == "expires_at":
                setattr(token, key, value)
        if scope_changed:
            await scope_check(db, owner, token.scope_mode, token.mcp_ids)
        await s.flush()
        result = public(token)
        revoke = (token.disabled or expired(token.expires_at) or scope_changed
                  or "discovery_mode" in values or "enable_resource_tools" in values
                  or "enable_mcp_proposals" in values)
    if revoke:
        await revoke_token_leases(request, token_id)
    return result


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: str, request: Request, user=CURRENT_USER):
    async with request.app.state.db.locked() as s:
        user = await revalidate_user(request, s)
        token = await owned_token(s, token_id, user)
        await s.delete(token)
    await revoke_token_leases(request, token_id)
    return {"ok": True}


@router.post("/tokens/{token_id}/rotate")
async def rotate_token(token_id: str, request: Request, response: Response, user=CURRENT_USER):
    async with request.app.state.db.locked() as s:
        user = await revalidate_user(request, s)
        token = await owned_token(s, token_id, user)
        secret, token.token_hash, token.prefix = mint_token()
        token.token_secret = encrypt_token_secret(request.app.state.config, secret)
        result = dict(public(token), token=secret)
    await revoke_token_leases(request, token_id)
    response.headers["Cache-Control"] = "no-store"
    return result
