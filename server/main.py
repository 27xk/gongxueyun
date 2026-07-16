import os
import time
import logging
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import text
from server.database import (
    create_db_and_tables,
    engine,
    require_database_schema_current,
    should_run_runtime_schema_migrations,
    upgrade_database_schema_to_head,
)
from server.api import router
from server.scheduler import is_scheduler_running, start_scheduler
from server.admin_users import ensure_seed_admin_users
from server.queue_worker import is_queue_worker_running, start_queue_worker, stop_queue_worker
from server.runtime_mode import should_start_background_services
from server.observability import prometheus_metrics_text, record_http_request, runtime_metrics
from server.request_context import request_context
from server.security import apply_security_headers, is_production, require_metrics_access, should_reject_cookie_csrf

logger = logging.getLogger(__name__)


def _bool_env(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _should_auto_download_captcha_models() -> bool:
    configured = _bool_env("CAPTCHA_MODEL_AUTO_DOWNLOAD")
    if configured is not None:
        return configured
    app_env = (os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    return app_env not in {"prod", "production"}


def _should_expose_api_docs() -> bool:
    configured = _bool_env("EXPOSE_API_DOCS")
    if configured is not None:
        return configured
    app_env = (os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    return app_env not in {"prod", "production"}


def _split_env_list(value: str) -> list[str]:
    return [item.strip() for item in (value or "").replace(";", ",").split(",") if item.strip()]


def _origin_host(value: str) -> str:
    parsed = urlparse((value or "").strip())
    if not parsed.scheme or not parsed.hostname:
        return ""
    return parsed.hostname.lower()


def _resolve_trusted_hosts() -> list[str]:
    hosts: list[str] = []
    for value in _split_env_list(os.getenv("TRUSTED_HOSTS") or ""):
        lowered = value.lower()
        if lowered and lowered not in hosts:
            hosts.append(lowered)
    if hosts:
        return hosts

    for env_name in ("FRONTEND_ORIGINS", "CORS_ORIGINS"):
        for value in _split_env_list(os.getenv(env_name) or ""):
            host = _origin_host(value)
            if host and host not in hosts:
                hosts.append(host)

    if hosts:
        return hosts

    if is_production():
        if _bool_env("ALLOW_MISSING_TRUSTED_HOSTS"):
            logger.warning("TRUSTED_HOSTS is not configured; Host header validation is explicitly disabled")
            return []
        raise RuntimeError("TRUSTED_HOSTS or FRONTEND_ORIGINS/CORS_ORIGINS must be configured in production")
    return []


def _max_request_body_bytes() -> int:
    default = 8 * 1024 * 1024 if is_production() else 0
    raw = os.getenv("MAX_REQUEST_BODY_BYTES")
    if raw is not None and str(raw).strip():
        try:
            return max(0, min(int(str(raw).strip()), 10 * 1024 * 1024))
        except (TypeError, ValueError):
            return default
    return default


def _request_body_too_large(request: Request, limit: int) -> bool:
    if limit <= 0:
        return False
    if (getattr(request, "method", "") or "").upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return False
    headers = getattr(request, "headers", {}) or {}
    content_length = headers.get("content-length")
    if not content_length:
        return False
    try:
        return int(content_length) > limit
    except (TypeError, ValueError):
        return False


async def _cache_request_body_with_limit(request: Request, limit: int) -> bool:
    if limit <= 0:
        return False
    if (getattr(request, "method", "") or "").upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return False
    if _request_body_too_large(request, limit):
        return True

    body_parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            return True
        body_parts.append(chunk)

    setattr(request, "_body", b"".join(body_parts))
    return False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    on_startup()
    try:
        yield
    finally:
        on_shutdown()


app = FastAPI(
    title="AutoMoGuDing SaaS",
    lifespan=lifespan,
    docs_url="/docs" if _should_expose_api_docs() else None,
    redoc_url="/redoc" if _should_expose_api_docs() else None,
    openapi_url="/openapi.json" if _should_expose_api_docs() else None,
)

trusted_hosts = _resolve_trusted_hosts()
if trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

@app.middleware("http")
async def _security_headers(request, call_next):
    started = time.perf_counter()
    status_code = 500
    path = request.url.path or "/"
    with request_context(request.headers.get("x-request-id")) as request_id:
        try:
            max_body_bytes = _max_request_body_bytes()
            if await _cache_request_body_with_limit(request, max_body_bytes):
                resp = JSONResponse(status_code=413, content={"detail": "Request body too large"})
                status_code = int(resp.status_code or 0)
                apply_security_headers(resp)
                resp.headers["X-Request-ID"] = request_id
                return resp
            if should_reject_cookie_csrf(request):
                resp = JSONResponse(status_code=403, content={"detail": "CSRF origin rejected"})
            else:
                resp = await call_next(request)
            status_code = int(getattr(resp, "status_code", 0) or 0)
            apply_security_headers(resp)
            resp.headers["X-Request-ID"] = request_id
            return resp
        finally:
            if not path.startswith("/metrics"):
                duration_ms = int((time.perf_counter() - started) * 1000)
                record_http_request(
                    method=request.method,
                    path=path,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    request_id=request_id,
                )

def _strip_wrapping(s: str) -> str:
    s2 = (s or "").strip()
    while len(s2) >= 2 and s2[0] == s2[-1] and s2[0] in ["'", '"', "`"]:
        s2 = s2[1:-1].strip()
    return s2

def _parse_origins(s: str) -> list[str]:
    raw = _strip_wrapping(s)
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(","):
        v = _strip_wrapping(part)
        if v:
            out.append(v)
    return out


def _resolve_cors_origins() -> list[str]:
    app_env = (os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    origins_env = os.getenv("CORS_ORIGINS") or os.getenv("FRONTEND_ORIGINS") or ""
    origins = _parse_origins(origins_env)
    if "*" in origins and app_env in ["prod", "production"] and not _bool_env("ALLOW_WILDCARD_CORS"):
        raise RuntimeError("Wildcard CORS origins are not allowed in production")
    if origins:
        return origins
    if app_env not in ["prod", "production"]:
        return ["http://localhost:5173", "http://127.0.0.1:5173"]
    return []


origins = _resolve_cors_origins()

if origins:
    allow_all = len(origins) == 1 and origins[0] == "*"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False if allow_all else True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
def on_startup():
    if should_run_runtime_schema_migrations():
        upgrade_database_schema_to_head()
        create_db_and_tables()
        require_database_schema_current(engine)
    else:
        require_database_schema_current(engine)
        logger.info("runtime schema migrations are disabled; run alembic before startup")
    ensure_seed_admin_users()

    if _should_auto_download_captcha_models():
        try:
            from server.util.CaptchaUtils import ensure_model_exists, MODEL_URLS

            for filename, url in MODEL_URLS.items():
                ensure_model_exists(filename, url)
        except Exception as e:
            logger.warning("failed to download captcha models: %s", e)

    if should_start_background_services():
        start_scheduler()
        start_queue_worker()


def on_shutdown():
    stop_queue_worker()


app.include_router(router, prefix="/api")


def _background_services_ready() -> bool:
    if not should_start_background_services():
        return True
    return is_scheduler_running() and is_queue_worker_running()


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "background_services": should_start_background_services(),
        "background_ready": _background_services_ready(),
    }


@app.get("/readyz")
def readyz():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    background_ready = _background_services_ready()
    if not background_ready:
        raise HTTPException(status_code=503, detail="background services are not ready")
    return {
        "ok": True,
        "background_services": should_start_background_services(),
        "background_ready": background_ready,
    }


@app.get("/metrics")
def metrics(request: Request):
    require_metrics_access(request)
    return runtime_metrics()


@app.get("/metrics.prom")
def metrics_prometheus(request: Request):
    require_metrics_access(request)
    return PlainTextResponse(prometheus_metrics_text(), media_type="text/plain; version=0.0.4")

static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web/dist")
if os.path.exists(static_dir):
    index_path = os.path.join(static_dir, "index.html")
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    @app.middleware("http")
    async def _spa_fallback(request: Request, call_next):
        resp = await call_next(request)
        if resp.status_code != 404:
            return resp
        if request.method != "GET":
            return resp
        path = request.url.path or "/"
        if path.startswith("/api"):
            return resp
        if path in ["/docs", "/redoc", "/openapi.json"]:
            return resp
        accept = (request.headers.get("accept") or "").lower()
        if "text/html" not in accept and "*/*" not in accept:
            return resp
        if not os.path.exists(index_path):
            return resp
        return FileResponse(index_path)
