import base64
import hashlib
import hmac
import json
import os
import time
import secrets
import logging
from functools import lru_cache
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from server.models import DEFAULT_TENANT_ID
from server.security import (
    CSRF_COOKIE_NAME,
    client_ip_from_request,
    env_flag,
    env_flag_default,
    generate_csrf_token,
    is_production,
    require_safe_app_secret,
)

_bearer = HTTPBearer(auto_error=False)
_SECRET_CACHE: bytes | None = None
logger = logging.getLogger(__name__)
ROLE_PERMISSIONS = {
    "admin": {
        "audit:read",
        "audit:purge",
        "admin_users:manage",
        "settings:read",
        "settings:manage",
        "users:read",
        "users:write",
        "users:delete",
        "tasks:run",
        "batch:read",
        "batch:manage",
    },
    "operator": {
        "audit:read",
        "settings:read",
        "users:read",
        "users:write",
        "tasks:run",
        "batch:read",
        "batch:manage",
    },
    "viewer": {
        "audit:read",
        "settings:read",
        "users:read",
        "batch:read",
    },
    "user": {
        "app:self",
        "tasks:run",
    },
}


@lru_cache(maxsize=16)
def _parsed_role_permissions_override(raw: str | None) -> dict[str, set[str]]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        logger.warning("Ignoring invalid ROLE_PERMISSIONS_JSON override")
        return {}
    if not isinstance(payload, dict):
        logger.warning("Ignoring non-object ROLE_PERMISSIONS_JSON override")
        return {}
    override: dict[str, set[str]] = {}
    for role, permissions in payload.items():
        role_name = str(role or "").strip()
        if not role_name:
            continue
        if not isinstance(permissions, (list, tuple, set)):
            logger.warning("Ignoring ROLE_PERMISSIONS_JSON entry for role %s", role_name)
            continue
        normalized = {str(item or "").strip() for item in permissions if str(item or "").strip()}
        if normalized:
            override[role_name] = normalized
    return override


def _role_permissions_override() -> dict[str, set[str]]:
    return _parsed_role_permissions_override(os.getenv("ROLE_PERMISSIONS_JSON"))

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("utf-8"))

def _secret() -> bytes:
    global _SECRET_CACHE
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE
    key = (os.getenv("APP_SECRET") or "").strip()
    if key:
        require_safe_app_secret(key)
        _SECRET_CACHE = key.encode("utf-8")
        return _SECRET_CACHE

    app_env = (os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    require = (os.getenv("REQUIRE_APP_SECRET") or "").strip().lower() in ["1", "true", "yes", "on"]
    if require or app_env in ["prod", "production"]:
        raise RuntimeError("APP_SECRET 未配置（生产环境必须设置）")

    _SECRET_CACHE = secrets.token_bytes(32)
    return _SECRET_CACHE

def hash_password(password: str) -> str:
    pw = (password or "").encode("utf-8")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw, salt, 200_000)
    return f"pbkdf2_sha256$200000${_b64url_encode(salt)}${_b64url_encode(dk)}"

def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iter_s, salt_b64, dk_b64 = (password_hash or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = _b64url_decode(salt_b64)
        expected = _b64url_decode(dk_b64)
        pw = (password or "").encode("utf-8")
        got = hashlib.pbkdf2_hmac("sha256", pw, salt, iterations)
        return hmac.compare_digest(got, expected)
    except Exception:
        return False

def issue_token(
    subject: str,
    role: str,
    ttl_seconds: int = 12 * 60 * 60,
    tenant_id: str = DEFAULT_TENANT_ID,
    token_version: int | None = None,
    extra_claims: dict | None = None,
) -> str:
    payload = {
        "sub": subject,
        "role": role,
        "tenant_id": tenant_id or DEFAULT_TENANT_ID,
        "exp": int(time.time()) + int(ttl_seconds),
    }
    if token_version is not None:
        payload["ver"] = int(token_version or 0)
    for key, value in (extra_claims or {}).items():
        if key not in {"sub", "role", "tenant_id", "exp", "ver"}:
            payload[key] = value
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(_secret(), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"

def verify_token(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 2:
            raise ValueError("bad token")
        payload_b64, sig_b64 = parts
        expected_sig = hmac.new(_secret(), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected_sig, _b64url_decode(sig_b64)):
            raise ValueError("bad signature")
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        exp = int(payload.get("exp") or 0)
        if exp <= int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")


def _allow_legacy_tokens() -> bool:
    configured = os.getenv("ALLOW_LEGACY_TOKENS")
    if configured is not None:
        return env_flag("ALLOW_LEGACY_TOKENS")
    return not is_production()


def validate_token_subject(payload: dict, db_engine=None) -> dict:
    tenant_id = tenant_id_from_payload(payload)
    if "ver" not in (payload or {}):
        if not _allow_legacy_tokens():
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        if db_engine is not None:
            require_active_tenant(tenant_id, db_engine=db_engine)
        return payload
    require_active_tenant(tenant_id, db_engine=db_engine)
    try:
        expected_version = int(payload.get("ver") or 0)
    except Exception:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")

    from sqlmodel import Session, select
    from server.models import AdminUser, AppUser

    if db_engine is None:
        from server.database import engine as db_engine

    role = str(payload.get("role") or "")
    subject = str(payload.get("sub") or "")
    with Session(db_engine) as session:
        if role == "user":
            if not subject.startswith("app:"):
                raise HTTPException(status_code=401, detail="未登录或登录已过期")
            try:
                app_user_id = int(subject.split(":", 1)[1])
            except Exception:
                raise HTTPException(status_code=401, detail="未登录或登录已过期")
            app_user = session.get(AppUser, app_user_id)
            if (
                not app_user
                or app_user.enabled is not True
                or str(app_user.tenant_id or DEFAULT_TENANT_ID) != tenant_id
                or int(app_user.token_version or 0) != expected_version
            ):
                raise HTTPException(status_code=401, detail="未登录或登录已过期")
            return payload

        if subject.startswith("app:"):
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        admin_user = session.exec(
            select(AdminUser).where((AdminUser.tenant_id == tenant_id) & (AdminUser.username == subject))
        ).first()
        if (
            not admin_user
            or admin_user.enabled is not True
            or admin_user.role != role
            or int(admin_user.token_version or 0) != expected_version
        ):
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        return payload

def revoke_token_subject(payload: dict | None, *, session=None, db_engine=None) -> bool:
    if not payload:
        return False
    from sqlmodel import Session, select
    from server.models import AdminUser, AppUser

    def _revoke(active_session) -> bool:
        role = str(payload.get("role") or "")
        subject = str(payload.get("sub") or "")
        tenant_id = tenant_id_from_payload(payload)
        if role == "user":
            if not subject.startswith("app:"):
                return False
            try:
                app_user_id = int(subject.split(":", 1)[1])
            except Exception:
                return False
            app_user = active_session.get(AppUser, app_user_id)
            if not app_user or str(app_user.tenant_id or DEFAULT_TENANT_ID) != tenant_id:
                return False
            app_user.token_version = int(app_user.token_version or 0) + 1
            active_session.add(app_user)
            return True

        if subject.startswith("app:"):
            return False
        admin_user = active_session.exec(
            select(AdminUser).where((AdminUser.tenant_id == tenant_id) & (AdminUser.username == subject))
        ).first()
        if not admin_user:
            return False
        admin_user.token_version = int(admin_user.token_version or 0) + 1
        active_session.add(admin_user)
        return True

    if session is not None:
        ok = _revoke(session)
        if ok:
            session.commit()
        return ok

    if db_engine is None:
        from server.database import engine as db_engine
    with Session(db_engine) as active_session:
        ok = _revoke(active_session)
        if ok:
            active_session.commit()
        return ok

def require_roles(payload: dict, roles: list[str]) -> dict:
    if payload.get("role") not in roles:
        raise HTTPException(status_code=403, detail="权限不足")
    return payload


def permissions_for_role(role: str) -> set[str]:
    role_name = str(role or "")
    override = _role_permissions_override().get(role_name)
    if override is not None:
        return set(override)
    return set(ROLE_PERMISSIONS.get(role_name, set()))


def permissions_for_payload(payload: dict | None) -> set[str]:
    permissions = permissions_for_role(str((payload or {}).get("role") or ""))
    permissions.discard("tenants:read")
    permissions.discard("tenants:manage")
    return permissions


def has_permission(payload: dict, permission: str) -> bool:
    return str(permission or "") in permissions_for_payload(payload)


def tenant_id_from_payload(payload: dict | None) -> str:
    return str((payload or {}).get("tenant_id") or DEFAULT_TENANT_ID)


def require_active_tenant(tenant_id: str | None, *, session=None, db_engine=None):
    normalized = str(tenant_id or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID

    from sqlmodel import Session
    from server.models import Tenant

    def _check(active_session):
        tenant = active_session.get(Tenant, normalized)
        if tenant is None:
            if normalized == DEFAULT_TENANT_ID:
                return None
            raise HTTPException(status_code=404, detail="Tenant not found")
        if str(tenant.status or "active").lower() != "active":
            raise HTTPException(status_code=403, detail="Tenant is disabled")
        return tenant

    if session is not None:
        return _check(session)
    if db_engine is None:
        from server.database import engine as db_engine
    with Session(db_engine) as active_session:
        return _check(active_session)


@lru_cache(maxsize=128)
def require_permission(permission: str):
    permission_name = str(permission or "")

    def _dependency(payload: dict = Depends(get_auth_payload)) -> dict:
        if not has_permission(payload, permission_name):
            raise HTTPException(status_code=403, detail="权限不足")
        return payload

    return _dependency

def _cookie_name_for_role(role: str) -> str:
    return "app_auth_token" if role == "user" else "admin_auth_token"

def set_auth_cookie(response, token: str, role: str) -> None:
    secure = env_flag_default("AUTH_COOKIE_SECURE", is_production())
    response.set_cookie(
        key=_cookie_name_for_role(role),
        value=token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=12 * 60 * 60,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=generate_csrf_token(),
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=12 * 60 * 60,
        path="/",
    )

def clear_auth_cookie(response, role: str) -> None:
    response.delete_cookie(key=_cookie_name_for_role(role), path="/")

def _extract_auth_token(credentials: HTTPAuthorizationCredentials = None, request: Request = None) -> str:
    token = credentials.credentials if credentials and credentials.credentials else ""
    if not token and request is not None:
        path = request.url.path or ""
        if path.startswith("/api/app") or path.startswith("/app"):
            token = request.cookies.get("app_auth_token") or request.cookies.get("admin_auth_token") or ""
        else:
            token = request.cookies.get("admin_auth_token") or request.cookies.get("app_auth_token") or ""
    return token

def get_auth_payload(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    request: Request = None,
) -> dict:
    token = _extract_auth_token(credentials, request)
    if not token:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return validate_token_subject(verify_token(token))

def get_optional_auth_payload(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    request: Request = None,
) -> dict | None:
    token = _extract_auth_token(credentials, request)
    if not token:
        return None
    try:
        return validate_token_subject(verify_token(token))
    except HTTPException:
        return None

def get_admin(payload: dict = Depends(get_auth_payload)) -> dict:
    return require_roles(payload, ["admin"])

def get_operator(payload: dict = Depends(get_auth_payload)) -> dict:
    return require_roles(payload, ["admin", "operator"])

def get_viewer(payload: dict = Depends(get_auth_payload)) -> dict:
    return require_roles(payload, ["admin", "operator", "viewer"])

def get_user(payload: dict = Depends(get_auth_payload)) -> dict:
    return require_roles(payload, ["user"])

def get_client_ip(request: Request) -> str:
    return client_ip_from_request(request)
