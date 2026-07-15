from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlmodel import Session, select
from sqlalchemy import func
from server.database import get_session, engine
from server.batch_jobs import get_batch_job_item_status_counts
from server.models import DEFAULT_TENANT_ID, User, UserCreate, UserRead, UserUpdate, UserListRead, AuditLog, BatchJob, BatchJobItem, AdminUser, AppUser, SystemSetting, ClockInPreauthorization
from server.scheduler import add_user_job, remove_user_job, user_to_config
from server.task_runner import perform_clock_in, perform_clock_in_makeup, perform_clock_in_makeup_many, run_task_by_config
from server.clockin_backfill import build_missing_clockin_day_options, normalize_clockin_records, parse_clockin_date
from server.clockin_preauthorization import (
    BEIJING_TZ,
    REGISTRATION_TICKET_TTL_SECONDS,
    build_alipay_open_urls,
    build_preauthorization_hooks,
    complete_preauthorization,
    iso_utc_datetime,
    issue_registration_ticket,
    list_preauthorizations,
    parse_plan_end_date,
    validate_preauthorization_target,
)
from server.util.Config import ConfigManager
from server.coreApi.MainLogicApi import ApiClient
from server.coreApi.AiServiceClient import generate_article, _ai_endpoint_detail, get_ai_prompt_version
from server.ai_governance import check_ai_generation_quota
from server.ai_settings import load_global_ai_settings, merge_ai_settings, sanitize_ai_settings_for_read, save_global_ai_settings, validate_ai_settings
from server.idempotency import (
    build_idempotency_request_hash,
    claim_idempotency_record,
    finalize_idempotency_record,
    peek_idempotency_response,
)
from typing import List, Any, Dict, Optional
import datetime
import hashlib
import json
import logging
import requests
from pydantic import BaseModel
from urllib.parse import urljoin
import time
import os
import re
import threading
from collections import OrderedDict
from server.auth import clear_auth_cookie, get_auth_payload, get_user, issue_token, get_optional_auth_payload, get_client_ip, permissions_for_payload, require_active_tenant, require_permission, revoke_token_subject, set_auth_cookie, tenant_id_from_payload, verify_password, hash_password
from server.rate_limit import check_rate_limit
from server.security import env_flag, int_env, is_production, is_safe_outbound_url, require_password_strength
from server.secret_store import decrypt_secret, encrypt_secret
from server.time_utils import utc_now
from server.user_runtime import apply_execution_results_to_user, normalize_push_notifications, normalize_smtp_settings, runtime_login_valid, runtime_plan_required, sanitize_execution_results, sync_runtime_fields_to_user
from server.util.MessagePush import send_test_smtp_message
from server.proxy_settings import (
    PROXY_SETTINGS_KEY,
    encode_proxy_settings,
    load_global_proxy_settings,
    normalize_proxy_settings,
)
from server.http_client import safe_external_error_detail
from server.settings_store import get_setting, upsert_setting

router = APIRouter()
logger = logging.getLogger(__name__)
require_audit_read = require_permission("audit:read")
require_audit_purge = require_permission("audit:purge")
require_admin_manage = require_permission("admin_users:manage")
require_settings_read = require_permission("settings:read")
require_settings_manage = require_permission("settings:manage")
require_users_read = require_permission("users:read")
require_users_write = require_permission("users:write")
require_users_delete = require_permission("users:delete")
require_tasks_run = require_permission("tasks:run")
require_batch_read = require_permission("batch:read")
require_batch_manage = require_permission("batch:manage")


def require_geocode_search_access(payload: dict = Depends(get_auth_payload)) -> dict:
    if payload.get("role") == "user":
        return payload
    if "users:write" not in permissions_for_payload(payload):
        raise HTTPException(status_code=403, detail="权限不足")
    return payload

NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"
MAPCHAXUN_GEOCODE_URL = "https://www.mapchaxun.cn/api/getSolidAdress"
NOTIFICATION_SETTINGS_KEY = "notifications"
DEFAULT_REPORT_MAKEUP_BATCH_DELAY_SECONDS = 2.0
MAX_REPORT_MAKEUP_BATCH_DELAY_SECONDS = 30.0
IDEMPOTENCY_BATCH_RUN_SETTING_PREFIX = "idempotency:batch-run"
IDEMPOTENCY_MANUAL_SETTING_PREFIX = "idempotency:manual"
IDEMPOTENCY_KEY_MAX_LENGTH = 128
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
IDEMPOTENCY_STATUS_PENDING = "pending"
IDEMPOTENCY_STATUS_COMPLETED = "completed"
IDEMPOTENCY_STATUS_FAILED = "failed"
IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 3600
ALIPAY_OUT_REGISTER_NO_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,128}$")
CLOCKIN_PREAUTHORIZATION_ACTION_LIMIT = 30


def _report_makeup_batch_delay_seconds() -> float:
    raw = os.getenv("REPORT_MAKEUP_BATCH_DELAY_SECONDS") or os.getenv("CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS") or str(DEFAULT_REPORT_MAKEUP_BATCH_DELAY_SECONDS)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_REPORT_MAKEUP_BATCH_DELAY_SECONDS
    if value < 0:
        return 0.0
    return min(value, MAX_REPORT_MAKEUP_BATCH_DELAY_SECONDS)


def _normalize_idempotency_key(raw_key: Optional[str]) -> Optional[str]:
    if raw_key is None:
        return None
    key = str(raw_key).strip()
    if not key:
        return None
    if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH or not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key header")
    return key


def _batch_run_idempotency_storage_key(idempotency_key: str) -> str:
    return f"{IDEMPOTENCY_BATCH_RUN_SETTING_PREFIX}:{idempotency_key}"


def _manual_operation_idempotency_storage_key(operation: str, idempotency_key: str) -> str:
    return f"{IDEMPOTENCY_MANUAL_SETTING_PREFIX}:{operation}:{idempotency_key}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _batch_run_request_hash(*, ids: List[int], concurrency: int, tenant_id: str, actor: str) -> str:
    payload = {
        "actor": str(actor or ""),
        "concurrency": int(concurrency or 0),
        "ids": [int(x) for x in ids],
        "tenant_id": str(tenant_id or DEFAULT_TENANT_ID),
    }
    return build_idempotency_request_hash(payload)


def _operation_request_hash(payload: Any) -> str:
    return build_idempotency_request_hash(payload)


def _batch_run_idempotency_value(
    *,
    request_hash: str,
    status: str,
    response: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "fingerprint": request_hash,
        "status": status,
        "updated_at": utc_now().isoformat(timespec="seconds"),
    }
    if response is not None:
        value["response"] = response
    if error:
        value["error"] = str(error)[:500]
    return value


def _operation_idempotency_value(
    *,
    request_hash: str,
    status: str,
    response: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    return _batch_run_idempotency_value(
        request_hash=request_hash,
        status=status,
        response=response,
        error=error,
    )


def _batch_run_idempotency_record_expired(value: dict[str, Any]) -> bool:
    try:
        updated_at = datetime.datetime.fromisoformat(str(value.get("updated_at") or "").replace("Z", "+00:00"))
    except Exception:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
    ttl_seconds = int_env("IDEMPOTENCY_TTL_SECONDS", IDEMPOTENCY_TTL_SECONDS, min_value=300, max_value=30 * 24 * 3600)
    return (utc_now() - updated_at).total_seconds() > ttl_seconds


def _batch_run_idempotency_record(session: Session, tenant_id: str, idempotency_key: str) -> SystemSetting | None:
    return get_setting(session, _batch_run_idempotency_storage_key(idempotency_key), tenant_id)


def _manual_operation_idempotency_record(
    session: Session,
    *,
    tenant_id: str,
    operation: str,
    idempotency_key: str,
) -> SystemSetting | None:
    return get_setting(session, _manual_operation_idempotency_storage_key(operation, idempotency_key), tenant_id)


def _batch_run_existing_response(
    session: Session,
    *,
    tenant_id: str,
    idempotency_key: str,
    request_hash: str,
) -> Optional[dict[str, Any]]:
    return peek_idempotency_response(
        db_engine=engine,
        tenant_id=tenant_id,
        logical_key=_batch_run_idempotency_storage_key(idempotency_key),
        request_hash=request_hash,
        conflict_detail="Idempotency key already used with different batch parameters",
        pending_detail="Batch request is already being processed",
    )


def _manual_operation_existing_response(
    session: Session,
    *,
    tenant_id: str,
    operation: str,
    idempotency_key: str,
    request_hash: str,
) -> Optional[dict[str, Any]]:
    return peek_idempotency_response(
        db_engine=engine,
        tenant_id=tenant_id,
        logical_key=_manual_operation_idempotency_storage_key(operation, idempotency_key),
        request_hash=request_hash,
        conflict_detail="Idempotency key already used with different request parameters",
        pending_detail="Request is already being processed",
    )


def _notification_settings_row(session: Session, tenant_id: str = DEFAULT_TENANT_ID) -> SystemSetting | None:
    return get_setting(session, NOTIFICATION_SETTINGS_KEY, tenant_id)


def _decode_smtp_settings(raw: Any) -> Dict[str, Any]:
    data = dict(raw) if isinstance(raw, dict) else {}
    if "password" in data:
        try:
            data["password"] = decrypt_secret(str(data.get("password") or ""))
        except Exception:
            data["password"] = ""
    return data


def _get_notification_settings(session: Session, tenant_id: str = DEFAULT_TENANT_ID) -> Dict[str, Any]:
    row = _notification_settings_row(session, tenant_id)
    value = row.value if row and isinstance(row.value, dict) else {}
    smtp = normalize_smtp_settings(_decode_smtp_settings((value or {}).get("smtp")))
    return {"smtp": smtp}


def _sanitize_notification_settings_for_read(settings: Dict[str, Any]) -> Dict[str, Any]:
    return {"smtp": normalize_smtp_settings(settings.get("smtp"))}


def _save_notification_settings(session: Session, smtp_payload: Dict[str, Any], tenant_id: str = DEFAULT_TENANT_ID) -> Dict[str, Any]:
    current = _get_notification_settings(session, tenant_id)
    smtp = normalize_smtp_settings(smtp_payload, current.get("smtp"))
    if not str(smtp.get("username") or "").strip():
        raise HTTPException(status_code=400, detail="请填写 QQ 邮箱")
    if not str(smtp.get("from") or "").strip():
        raise HTTPException(status_code=400, detail="请填写发件人名称")
    if not str(smtp.get("password") or "").strip():
        raise HTTPException(status_code=400, detail="请填写授权码")

    stored_smtp = dict(smtp)
    stored_smtp["password"] = encrypt_secret(str(stored_smtp.get("password") or ""))
    upsert_setting(session, NOTIFICATION_SETTINGS_KEY, {"smtp": stored_smtp}, tenant_id)
    return {"smtp": smtp}


def _save_proxy_settings(session: Session, proxy_payload: Dict[str, Any], tenant_id: str = DEFAULT_TENANT_ID) -> Dict[str, Any]:
    proxy = normalize_proxy_settings(proxy_payload)
    if proxy.get("enabled") and not str(proxy.get("apiUrl") or "").strip() and not str(proxy.get("proxyUrls") or "").strip():
        raise HTTPException(status_code=400, detail="启用代理时请填写动态代理接口或静态代理列表")
    upsert_setting(session, PROXY_SETTINGS_KEY, encode_proxy_settings(proxy), tenant_id)
    return proxy

def _is_safe_outbound_url(url: str) -> bool:
    return is_safe_outbound_url(url, allow_private=env_flag("ALLOW_PRIVATE_AI_TEST"))


def _ai_audit_detail(config: ConfigManager, title: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ai_cfg = config.get_value("config.ai")
    if not isinstance(ai_cfg, dict):
        ai_cfg = {}
    detail = {
        "title": title,
        "endpoint": _ai_endpoint_detail(str(ai_cfg.get("apiUrl") or "")),
        "model": str(ai_cfg.get("model") or ""),
        "prompt_version": get_ai_prompt_version(),
    }
    if extra:
        detail.update(extra)
    return detail


def _apply_global_ai_settings(config_data: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
    with Session(engine) as session:
        ai_settings = load_global_ai_settings(session, tenant_id)
    if str(ai_settings.get("apiUrl") or "").strip() or str(ai_settings.get("apikey") or "").strip() or str(ai_settings.get("model") or "").strip():
        config_data.setdefault("config", {})["ai"] = ai_settings
    return config_data


def _login_payload(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    if env_flag("RETURN_AUTH_TOKEN"):
        data["token"] = token
    return data


def _app_registration_enabled() -> bool:
    configured = os.getenv("APP_REGISTRATION_ENABLED")
    if configured is not None:
        return env_flag("APP_REGISTRATION_ENABLED")
    return not is_production()


def _ensure_app_registration_allowed(session: Session, tenant_id: str) -> None:
    tenant = require_active_tenant(tenant_id, session=session)
    if not _app_registration_enabled():
        raise HTTPException(status_code=403, detail="App registration is disabled")
    settings = tenant.settings if tenant is not None and isinstance(tenant.settings, dict) else {}
    if settings.get("registration_enabled") is False:
        raise HTTPException(status_code=403, detail="App registration is disabled for this tenant")

def _resolve_legacy_app_password_hash(user: User, current_hash: str | None = None) -> str:
    if user.app_password_hash:
        return str(user.app_password_hash)
    try:
        raw_password = decrypt_secret(str(user.password or ""))
    except Exception:
        raw_password = str(user.password or "")
    if not raw_password:
        return ""
    if current_hash and verify_password(raw_password, current_hash):
        return current_hash
    return hash_password(raw_password)

def _sanitize_user_for_read(user: User) -> Dict[str, Any]:
    data = UserRead.model_validate(user).model_dump()
    try:
        data["password"] = decrypt_secret(str(data.get("password") or ""))
    except Exception:
        data["password"] = ""
    data["pushNotifications"] = normalize_push_notifications(data.get("pushNotifications"))
    return data

def _sanitize_user_for_self(user: User) -> Dict[str, Any]:
    data = UserRead.model_validate(user).model_dump()
    try:
        data["password"] = decrypt_secret(str(data.get("password") or ""))
    except Exception:
        data["password"] = ""
    if "app_password_hash" in data:
        data["app_password_hash"] = None
    data["pushNotifications"] = normalize_push_notifications(data.get("pushNotifications"))
    return data


def _is_deleted_user(user: User | None) -> bool:
    return bool(user is not None and getattr(user, "deleted_at", None) is not None)


def _tenant_matches(row_tenant_id: Optional[str], expected_tenant_id: str) -> bool:
    if expected_tenant_id == DEFAULT_TENANT_ID and not row_tenant_id:
        return True
    return str(row_tenant_id or "") == expected_tenant_id


def _tenant_filter(column, tenant_id: str):
    if tenant_id == DEFAULT_TENANT_ID:
        return (column == tenant_id) | (column.is_(None))
    return column == tenant_id


def _get_active_user_or_404(session: Session, user_id: int, tenant_id: str = DEFAULT_TENANT_ID) -> User:
    user = session.get(User, user_id)
    if not user or _is_deleted_user(user) or not _tenant_matches(getattr(user, "tenant_id", None), tenant_id):
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _get_active_user_for_payload(session: Session, user_id: int, payload: dict | None) -> User:
    return _get_active_user_or_404(session, user_id, tenant_id_from_payload(payload))


def _ensure_remote_runtime(api_client: ApiClient, config: ConfigManager) -> None:
    if not runtime_login_valid(config.get_value("userInfo")):
        api_client.login()
    if runtime_plan_required(config.config):
        api_client.fetch_internship_plan()

def _get_authed_app_user(*, session: Session, payload: dict) -> AppUser:
    sub = str(payload.get("sub") or "")
    if sub.startswith("app:"):
        try:
            app_user_id = int(sub.split(":", 1)[1])
        except Exception:
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        app_user = session.get(AppUser, app_user_id)
        if not app_user:
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        if app_user.enabled is not True:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        if not _tenant_matches(getattr(app_user, "tenant_id", None), tenant_id_from_payload(payload)):
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        return app_user

    if not sub.startswith("user:"):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    try:
        legacy_user_id = int(sub.split(":", 1)[1])
    except Exception:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    legacy_user = session.get(User, legacy_user_id)
    if not legacy_user or _is_deleted_user(legacy_user):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    if legacy_user.app_enabled is not True:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    if not legacy_user.app_password_hash:
        raise HTTPException(status_code=403, detail="账号未启用用户端登录")
    tenant_id = tenant_id_from_payload(payload)
    if not _tenant_matches(getattr(legacy_user, "tenant_id", None), tenant_id):
        raise HTTPException(status_code=401, detail="Unauthorized")
    app_user = session.exec(
        select(AppUser).where((AppUser.tenant_id == tenant_id) & (AppUser.phone == legacy_user.phone))
    ).first()
    if not app_user:
        app_user = AppUser(
            tenant_id=tenant_id,
            phone=legacy_user.phone,
            password_hash=legacy_user.app_password_hash,
            enabled=bool(legacy_user.app_enabled),
            bound_user_id=legacy_user.id,
        )
        session.add(app_user)
        session.commit()
        session.refresh(app_user)
    if app_user.enabled is not True:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    return app_user

def _get_bound_task_user(*, session: Session, app_user: AppUser) -> User:
    if not app_user.bound_user_id:
        raise HTTPException(status_code=403, detail="请先绑定工学云账号")
    user = session.get(User, int(app_user.bound_user_id))
    if not user or _is_deleted_user(user):
        raise HTTPException(status_code=403, detail="绑定信息已失效，请重新绑定工学云账号")
    if not _tenant_matches(getattr(user, "tenant_id", None), str(app_user.tenant_id or DEFAULT_TENANT_ID)):
        raise HTTPException(status_code=403, detail="Bound task user tenant mismatch")
    return user

def _any_report_enabled(user: User) -> bool:
    rs = user.reportSettings if isinstance(user.reportSettings, dict) else {}
    for k in ["daily", "weekly", "monthly"]:
        part = rs.get(k)
        if isinstance(part, dict) and part.get("enabled") is True:
            return True
    return False

class AppRegisterRequest(BaseModel):
    phone: str
    password: str

class AppLoginRequest(BaseModel):
    phone: str
    password: str

class AppReportSubmitRequest(BaseModel):
    content: str
    target_period: Optional[str] = None

class AppMeResponse(BaseModel):
    app_phone: str
    bound: bool
    task_user: Optional[Dict[str, Any]] = None

class AppRunRequest(BaseModel):
    task_type: Optional[str] = None
    force_report: Optional[bool] = None
    target_period: Optional[str] = None


REPORT_RUN_TASK_TYPES = {"report", "daily_report", "weekly_report", "monthly_report"}


def _should_rate_limit_run_request(req: Optional[AppRunRequest]) -> bool:
    task_type = str(getattr(req, "task_type", "") or "").strip()
    return task_type not in REPORT_RUN_TASK_TYPES


class ClockInMakeupRequest(BaseModel):
    target_date: Optional[str] = None
    target_dates: Optional[List[str]] = None
    target_type: Optional[str] = None


class AlipayClockInContinueRequest(BaseModel):
    out_register_no: str
    target_type: str = "START"


class ClockInPreauthorizationStartRequest(BaseModel):
    target_date: str
    target_type: str


class ClockInPreauthorizationCompleteRequest(BaseModel):
    registration_ticket: str


class ClockInPreauthorizationItemResponse(BaseModel):
    id: Optional[int] = None
    target_date: str
    target_type: str
    target_time: Optional[str] = None
    status: str
    authorized_at: Optional[str] = None
    consumed_at: Optional[str] = None
    used_target_type: Optional[str] = None
    can_authorize: Optional[bool] = None


class ClockInPreauthorizationListResponse(BaseModel):
    account: str
    added_date: str
    plan_end_date: str
    schedule: Dict[str, str]
    scope: str
    summary: Dict[str, int]
    page: int
    page_size: int
    total: int
    items: List[ClockInPreauthorizationItemResponse]


class ClockInPreauthorizationStartResponse(BaseModel):
    registration_ticket: str
    direct_url: str
    browser_url: str
    started_at: str
    expires_at: str


class ClockInPreauthorizationCompleteResponse(BaseModel):
    item: ClockInPreauthorizationItemResponse


REPORT_META = {
    "daily": {
        "report_type": "day",
        "task_type": "daily_report",
        "task_name": "日报",
        "paper_num_key": "planInfo.planPaper.dayPaperNum",
        "form_type": 7,
    },
    "weekly": {
        "report_type": "week",
        "task_type": "weekly_report",
        "task_name": "周报",
        "paper_num_key": "planInfo.planPaper.weekPaperNum",
        "form_type": 8,
    },
    "monthly": {
        "report_type": "month",
        "task_type": "monthly_report",
        "task_name": "月报",
        "paper_num_key": "planInfo.planPaper.monthPaperNum",
        "form_type": 9,
    },
}


def _get_report_meta(report_key: str) -> Dict[str, Any]:
    meta = REPORT_META.get(str(report_key or "").strip().lower())
    if not meta:
        raise HTTPException(status_code=404, detail="报告类型不存在")
    return meta


def _parse_report_target_for_api(report_type: str, target_period: Optional[str]) -> datetime.datetime:
    raw = str(target_period or "").strip()
    try:
        if report_type == "month" and raw:
            return datetime.datetime.strptime(raw[:7], "%Y-%m")
        if raw:
            return datetime.datetime.strptime(raw[:10], "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="补交周期格式错误")
    return datetime.datetime.now()


def _api_week_bounds(dt: datetime.datetime) -> tuple[str, str]:
    start = dt - datetime.timedelta(days=dt.weekday())
    end = start + datetime.timedelta(days=6)
    return start.strftime("%Y-%m-%d 00:00:00"), end.strftime("%Y-%m-%d 23:59:59")


def _api_period_date(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _api_period_month(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m")


def _api_month_add(dt: datetime.datetime, months: int) -> datetime.datetime:
    month_index = dt.year * 12 + dt.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    return dt.replace(year=year, month=month, day=1)


def _api_report_period_key(report: Dict[str, Any], report_type: str) -> str:
    try:
        if report_type == "day":
            ts = report.get("createTime") or report.get("reportTime")
            return str(ts or "")[:10]
        if report_type == "week":
            start = str(report.get("startTime") or "")[:10]
            if start:
                return start
            ts = str(report.get("reportTime") or "")[:10]
            if ts:
                dt = datetime.datetime.strptime(ts, "%Y-%m-%d")
                return _api_period_date(dt - datetime.timedelta(days=dt.weekday()))
        if report_type == "month":
            yearmonth = str(report.get("yearmonth") or "")[:7]
            if yearmonth:
                return yearmonth
            return str(report.get("reportTime") or "")[:7]
    except Exception:
        return ""
    return ""


def _api_parse_date_value(value: Any) -> Optional[datetime.datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("/", "-")
    if raw.isdigit() and len(raw) >= 13:
        try:
            return datetime.datetime.fromtimestamp(int(raw[:13]) / 1000)
        except Exception:
            return None
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y%m%d", 8)):
        try:
            return datetime.datetime.strptime(raw[:size], fmt)
        except Exception:
            continue
    match = re.search(r"20\d{2}-\d{1,2}-\d{1,2}", raw)
    if match:
        try:
            return datetime.datetime.strptime(match.group(0), "%Y-%m-%d")
        except Exception:
            return None
    return None


def _api_find_date_by_keys(data: Any, key_hints: List[str]) -> Optional[datetime.datetime]:
    if isinstance(data, dict):
        for key, value in data.items():
            key_lower = str(key or "").lower()
            if any(hint.lower() in key_lower for hint in key_hints):
                parsed = _api_parse_date_value(value)
                if parsed:
                    return parsed
        for value in data.values():
            parsed = _api_find_date_by_keys(value, key_hints)
            if parsed:
                return parsed
    elif isinstance(data, list):
        for value in data:
            parsed = _api_find_date_by_keys(value, key_hints)
            if parsed:
                return parsed
    return None


def _api_report_period_range(config_data: Dict[str, Any], submitted_keys: set[str], report_type: str) -> tuple[datetime.datetime, datetime.datetime]:
    now = datetime.datetime.now()
    start = _api_find_date_by_keys(
        config_data.get("planInfo"),
        ["start", "begin", "practiceStart", "practicebegin", "sxks", "kssj", "开始"],
    )
    end = _api_find_date_by_keys(
        config_data.get("planInfo"),
        ["end", "finish", "practiceEnd", "practicefinish", "sxjs", "jssj", "结束"],
    )
    schedule = ((config_data.get("config") or {}).get("clockIn") or {}).get("schedule") or {}
    if not start:
        start = _api_parse_date_value(schedule.get("startDate"))
    if not end and start:
        try:
            total_days = int(schedule.get("totalDays") or 0)
        except Exception:
            total_days = 0
        if total_days > 0:
            end = start + datetime.timedelta(days=total_days - 1)
    if not start and submitted_keys:
        first_key = sorted(submitted_keys)[0]
        start = _api_parse_date_value(first_key)
    if not start:
        start = now
    if not end or end > now:
        end = now
    if start > end:
        start = end
    if report_type == "week":
        start = start - datetime.timedelta(days=start.weekday())
    elif report_type == "month":
        start = start.replace(day=1)
        end = end.replace(day=1)
    return start, end


def _api_period_options(report_type: str, start_time: datetime.datetime, end_time: datetime.datetime) -> List[Dict[str, str]]:
    options: List[Dict[str, str]] = []
    if report_type == "day":
        dt = end_time
        while dt.date() >= start_time.date():
            value = _api_period_date(dt)
            label = f"{value}（今天）" if dt.date() == datetime.date.today() else value
            options.append({"value": value, "label": label})
            dt -= datetime.timedelta(days=1)
    elif report_type == "week":
        start_monday = start_time - datetime.timedelta(days=start_time.weekday())
        dt = end_time - datetime.timedelta(days=end_time.weekday())
        while dt.date() >= start_monday.date():
            start_dt = dt
            end_dt = start_dt + datetime.timedelta(days=6)
            value = _api_period_date(start_dt)
            label = f"{value} 至 {_api_period_date(end_dt)}"
            options.append({"value": value, "label": label})
            dt -= datetime.timedelta(days=7)
    elif report_type == "month":
        dt = end_time.replace(day=1)
        start_month = start_time.replace(day=1)
        while dt >= start_month:
            value = _api_period_month(dt)
            label = f"{value} 月"
            options.append({"value": value, "label": label})
            dt = _api_month_add(dt, -1)
    return options


def _clockin_schedule_weekdays(config_data: Dict[str, Any]) -> List[int]:
    clock_in = ((config_data.get("config") or {}).get("clockIn") or {})
    if not isinstance(clock_in, dict):
        return [1, 2, 3, 4, 5, 6, 7]
    schedule = clock_in.get("schedule") if isinstance(clock_in.get("schedule"), dict) else {}
    weekdays = schedule.get("weekdays")
    if weekdays is None:
        weekdays = clock_in.get("customDays")
    if not isinstance(weekdays, list):
        return [1, 2, 3, 4, 5, 6, 7]
    out: List[int] = []
    for item in weekdays:
        try:
            day = int(item)
        except Exception:
            continue
        if 1 <= day <= 7 and day not in out:
            out.append(day)
    return out


def _clockin_period_range(config_data: Dict[str, Any], normalized_records: List[Dict[str, Any]]) -> tuple[datetime.date, datetime.date]:
    now = datetime.datetime.now()
    start = _api_find_date_by_keys(
        config_data.get("planInfo"),
        ["start", "begin", "practiceStart", "practicebegin", "sxks", "kssj", "开始"],
    )
    end = _api_find_date_by_keys(
        config_data.get("planInfo"),
        ["end", "finish", "practiceEnd", "practicefinish", "sxjs", "jssj", "结束"],
    )
    schedule = (((config_data.get("config") or {}).get("clockIn") or {}).get("schedule") or {})
    if not isinstance(schedule, dict):
        schedule = {}
    if not start:
        start = _api_parse_date_value(schedule.get("startDate"))
    if not end and start:
        try:
            total_days = int(schedule.get("totalDays") or 0)
        except Exception:
            total_days = 0
        if total_days > 0:
            end = start + datetime.timedelta(days=total_days - 1)
    if not start and normalized_records:
        first_date = sorted({item["date"] for item in normalized_records if item.get("date")})[0]
        start = _api_parse_date_value(first_date)
    if not start:
        start = now.replace(day=1)
    if not end or end > now:
        end = now
    if start > end:
        start = end
    return start.date(), end.date()


def _get_missing_clockin_days_for_user(user: User) -> Dict[str, Any]:
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法获取打卡记录")
    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    _ensure_remote_runtime(api_client, config)
    start_date, end_date = _clockin_period_range(config_data, [])
    records = api_client.get_checkin_records(start_date, end_date)
    normalized_records = normalize_clockin_records(records)
    start_date, end_date = _clockin_period_range(config_data, normalized_records)
    options = build_missing_clockin_day_options(
        records,
        start_date,
        end_date,
        scheduled_weekdays=_clockin_schedule_weekdays(config_data),
        respect_scheduled_weekdays=False,
    )
    return {
        "ok": True,
        "options": options,
        "records": normalized_records[:100],
        "record_count": len(normalized_records),
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
    }


def _clockin_makeup_dates_from_request(req: ClockInMakeupRequest) -> List[str]:
    raw_dates: List[Any] = []
    if req.target_date:
        raw_dates.append(req.target_date)
    if req.target_dates:
        raw_dates.extend(req.target_dates)

    dates: List[str] = []
    seen = set()
    for item in raw_dates:
        target = parse_clockin_date(item)
        if not target:
            raise HTTPException(status_code=400, detail="补卡日期格式错误")
        value = target.strftime("%Y-%m-%d")
        if value not in seen:
            seen.add(value)
            dates.append(value)
    if not dates:
        raise HTTPException(status_code=400, detail="请选择补卡日期")
    return dates


def _clockin_makeup_type_from_request(req: ClockInMakeupRequest) -> str:
    target_type = str(req.target_type or "START").strip().upper()
    if target_type not in ("START", "END"):
        raise HTTPException(status_code=400, detail="补卡类型错误")
    return target_type


def _alipay_continue_values(req: AlipayClockInContinueRequest) -> tuple[str, str]:
    out_register_no = str(req.out_register_no or "").strip()
    target_type = str(req.target_type or "").strip().upper()
    if not ALIPAY_OUT_REGISTER_NO_PATTERN.fullmatch(out_register_no):
        raise HTTPException(status_code=400, detail="支付宝验证登记编号格式错误")
    if target_type not in ("START", "END"):
        raise HTTPException(status_code=400, detail="打卡类型错误")
    return out_register_no, target_type


def _preauthorization_target_values(
    req: ClockInPreauthorizationStartRequest,
) -> tuple[datetime.date, str]:
    try:
        target_date = datetime.date.fromisoformat(str(req.target_date or "").strip())
    except Exception:
        raise ValueError("预授权日期格式错误")
    target_type = str(req.target_type or "").strip().upper()
    if target_type not in {"START", "END", "MAKEUP"}:
        raise ValueError("预授权类型错误")
    return target_date, target_type


def _sync_preauthorization_plan(
    user: User,
    *,
    force: bool,
) -> tuple[ApiClient, Dict[str, Any]]:
    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    if force:
        if not runtime_login_valid(config.get_value("userInfo")):
            api_client.login()
        api_client.fetch_internship_plan()
    else:
        _ensure_remote_runtime(api_client, config)
    sync_runtime_fields_to_user(user, config_data)
    return api_client, config_data


def _list_clockin_preauthorizations_for_user(
    session: Session,
    user: User,
    *,
    scope: str,
    page: int,
    page_size: int,
    status: Optional[str],
    target_type: Optional[str],
    refresh_plan: bool,
) -> Dict[str, Any]:
    needs_plan = refresh_plan
    if not needs_plan:
        try:
            parse_plan_end_date(user.planInfo)
        except ValueError:
            needs_plan = True
    if needs_plan:
        _sync_preauthorization_plan(user, force=True)
        session.add(user)
        session.commit()
        session.refresh(user)
    return list_preauthorizations(
        session,
        user,
        scope=scope,
        page=page,
        page_size=page_size,
        status=status,
        target_type=target_type,
    )


def _start_clockin_preauthorization_for_user(
    user: User,
    req: ClockInPreauthorizationStartRequest,
    *,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    target_date, target_type = _preauthorization_target_values(req)
    try:
        parse_plan_end_date(user.planInfo)
    except ValueError:
        _sync_preauthorization_plan(user, force=True)
    validate_preauthorization_target(
        user,
        target_date=target_date,
        target_type=target_type,
    )
    if session is not None:
        existing = session.exec(
            select(ClockInPreauthorization).where(
                (ClockInPreauthorization.tenant_id == user.tenant_id)
                & (ClockInPreauthorization.user_id == user.id)
                & (ClockInPreauthorization.target_date == target_date)
                & (ClockInPreauthorization.target_type == target_type)
                & (ClockInPreauthorization.status == "authorized")
            )
        ).first()
        if existing is not None:
            raise ValueError("该日期已有有效预授权")

    api_client, config_data = _sync_preauthorization_plan(user, force=False)
    validate_preauthorization_target(
        user,
        target_date=target_date,
        target_type=target_type,
    )
    registration = api_client.create_alipay_clockin_verification()
    started_at = datetime.datetime.now(BEIJING_TZ)
    direct_url, browser_url = build_alipay_open_urls(
        str(registration.get("registerUrl") or ""),
        account=str(user.phone or ""),
        started_at=started_at,
    )
    registration_ticket = issue_registration_ticket(
        tenant_id=user.tenant_id,
        user_id=int(user.id),
        target_date=target_date,
        target_type=target_type,
        out_register_no=str(registration.get("outRegisterNo") or ""),
        started_at=started_at,
    )
    return {
        "registration_ticket": registration_ticket,
        "direct_url": direct_url,
        "browser_url": browser_url,
        "started_at": started_at.isoformat(),
        "expires_at": (
            started_at
            + datetime.timedelta(seconds=REGISTRATION_TICKET_TTL_SECONDS)
        ).isoformat(),
    }


def _serialize_preauthorization_record(
    row: ClockInPreauthorization,
) -> Dict[str, Any]:
    return {
        "id": row.id,
        "target_date": row.target_date.isoformat(),
        "target_type": row.target_type,
        "status": row.status,
        "authorized_at": iso_utc_datetime(row.authorized_at),
        "consumed_at": iso_utc_datetime(row.consumed_at),
        "used_target_type": row.used_target_type,
    }


def _complete_clockin_preauthorization_for_user(
    session: Session,
    user: User,
    registration_ticket: str,
) -> Dict[str, Any]:
    row = complete_preauthorization(
        session,
        user=user,
        ticket=str(registration_ticket or "").strip(),
    )
    return _serialize_preauthorization_record(row)


def _continue_alipay_clockin_for_user(
    user: User,
    out_register_no: str,
    target_type: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    _ensure_remote_runtime(api_client, config)
    result = perform_clock_in(
        api_client,
        config,
        forced_checkin_type=target_type,
        out_register_no=out_register_no,
    )
    apply_execution_results_to_user(user, [result], config_data)
    return result, config_data


def _makeup_clockin_for_user(user: User, target_dates: List[str], target_type: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    _ensure_remote_runtime(api_client, config)
    if hasattr(api_client, "enable_proxy"):
        api_client.enable_proxy()
    preauthorization_hooks = build_preauthorization_hooks(user, db_engine=engine)
    if len(target_dates) == 1:
        result = perform_clock_in_makeup(
            api_client,
            config,
            target_dates[0],
            target_type=target_type,
            preauthorization_hooks=preauthorization_hooks,
        )
    else:
        result = perform_clock_in_makeup_many(
            api_client,
            config,
            target_dates,
            target_type=target_type,
            preauthorization_hooks=preauthorization_hooks,
        )
    apply_execution_results_to_user(user, [result], config_data)
    return result, config_data


def _makeup_all_missing_clockin_for_user(user: User, target_type: str) -> tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    missing = _get_missing_clockin_days_for_user(user)
    target_dates = [
        item.get("value")
        for item in missing.get("options", [])
        if item.get("value") and target_type in (item.get("missing_types") or [])
    ]
    if not target_dates:
        config_data = user_to_config(user)
        type_label = {"START": "上班", "END": "下班"}.get(target_type, target_type)
        result = {
            "status": "skip",
            "message": f"暂无待补{type_label}日期",
            "task_type": "补卡",
            "details": {"补卡天数": 0, "补卡类型": type_label},
            "items": [],
        }
        apply_execution_results_to_user(user, [result], config_data)
        return result, config_data, []
    result, config_data = _makeup_clockin_for_user(user, target_dates, target_type)
    return result, config_data, target_dates


def _is_report_enabled_for_user(user: User, report_key: str) -> bool:
    settings = user.reportSettings if isinstance(user.reportSettings, dict) else {}
    report_settings = settings.get(report_key) if isinstance(settings.get(report_key), dict) else {}
    return bool(report_settings.get("enabled"))


def _get_missing_report_periods_for_user(user: User, report_key: str) -> Dict[str, Any]:
    meta = _get_report_meta(report_key)
    report_type = meta["report_type"]
    if not _is_report_enabled_for_user(user, report_key):
        return {
            "ok": True,
            "report_key": report_key,
            "report_type": report_type,
            "options": [],
            "submitted_count": 0,
            "disabled": True,
            "message": f"未开启{meta['task_name']}，无需获取未提交周期",
        }
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法获取已提交报告")
    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    _ensure_remote_runtime(api_client, config)
    submitted = api_client.get_all_submitted_reports_info(report_type) or {}
    submitted_reports = submitted.get("data", []) if isinstance(submitted, dict) else []
    submitted_keys = {
        key
        for key in (_api_report_period_key(report, report_type) for report in submitted_reports)
        if key
    }
    start_time, end_time = _api_report_period_range(config_data, submitted_keys, report_type)
    options = [
        item
        for item in _api_period_options(report_type, start_time, end_time)
        if item["value"] not in submitted_keys
    ]
    return {
        "ok": True,
        "report_key": report_key,
        "report_type": report_type,
        "options": options,
        "submitted_count": len(submitted_keys),
    }


def _same_api_report_period(report: Dict[str, Any], report_type: str, current_time: datetime.datetime, weeks_label: str) -> bool:
    try:
        if report_type == "day":
            return _api_report_period_key(report, report_type) == current_time.strftime("%Y-%m-%d")
        if report_type == "week":
            start, end = _api_week_bounds(current_time)
            report_start = str(report.get("startTime") or "")[:10]
            report_end = str(report.get("endTime") or "")[:10]
            if report_start or report_end:
                return report_start == start[:10] and report_end == end[:10]
            return _api_report_period_key(report, report_type) == start[:10]
        if report_type == "month":
            return _api_report_period_key(report, report_type) == current_time.strftime("%Y-%m")
    except Exception:
        return False
    return False


def _build_report_info(
    *,
    api_client: ApiClient,
    config: ConfigManager,
    meta: Dict[str, Any],
    content: str,
    target_period: Optional[str],
    submitted: Optional[Dict[str, Any]] = None,
    job_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    report_type = meta["report_type"]
    current_time = _parse_report_target_for_api(report_type, target_period)
    submitted_data = submitted if isinstance(submitted, dict) else api_client.get_submitted_reports_info(report_type) or {}
    submitted_reports = submitted_data.get("data", []) if isinstance(submitted_data, dict) else []
    count = (submitted_data.get("flag", 0) if isinstance(submitted_data, dict) else 0) + 1
    title = f"第{count}天日报" if report_type == "day" else (f"第{count}周周报" if report_type == "week" else f"第{count}月月报")
    weeks_label = f"第{count}周"
    if isinstance(submitted_reports, list):
        for report in submitted_reports:
            if _same_api_report_period(report, report_type, current_time, weeks_label):
                raise HTTPException(status_code=400, detail=f"该{meta['task_name']}周期已经提交过")
    job_data = job_info if isinstance(job_info, dict) else api_client.get_job_info()
    report_info = {
        "title": title,
        "content": content,
        "attachments": "",
        "reportType": report_type,
        "jobId": job_data.get("jobId", None),
        "reportTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
        "formFieldDtoList": api_client.get_from_info(int(meta["form_type"])),
    }
    if report_type == "week":
        start, end = _api_week_bounds(current_time)
        report_info["startTime"] = start
        report_info["endTime"] = end
        report_info["weeks"] = weeks_label
    elif report_type == "month":
        report_info["yearmonth"] = current_time.strftime("%Y-%m")
    return report_info


def _generate_report_content_for_user(user: User, report_key: str, target_period: Optional[str], generate_content: bool = True) -> Dict[str, Any]:
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法生成报告")
    meta = _get_report_meta(report_key)
    config_data = user_to_config(user)
    _apply_global_ai_settings(config_data, str(user.tenant_id or DEFAULT_TENANT_ID))
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    report_type = meta["report_type"]
    if generate_content:
        ai_cfg = config.get_value("config.ai")
        if not isinstance(ai_cfg, dict):
            raise HTTPException(status_code=400, detail="未配置 AI 参数")
        if not (str(ai_cfg.get("apikey") or "").strip()) or not (str(ai_cfg.get("apiUrl") or "").strip()) or not (str(ai_cfg.get("model") or "").strip()):
            raise HTTPException(status_code=400, detail="请先在 AI 设置中填写 API URL、API Key 和 Model")
        check_ai_generation_quota(tenant_id=user.tenant_id, user_id=user.id)

    _ensure_remote_runtime(api_client, config)
    submitted = api_client.get_submitted_reports_info(report_type) or {}
    count = (submitted.get("flag", 0) if isinstance(submitted, dict) else 0) + 1
    title = f"第{count}天日报" if report_type == "day" else (f"第{count}周周报" if report_type == "week" else f"第{count}月月报")
    job_info = api_client.get_job_info()
    content = ""
    if generate_content:
        content = generate_article(
            config,
            title,
            job_info,
            config.get_value(meta["paper_num_key"]),
            (submitted.get("data", []) if isinstance(submitted, dict) else [])[:4],
        )
    return {
        "config_data": config_data,
        "title": title,
        "content": content,
        "api_client": api_client,
        "config": config,
        "meta": meta,
        "submitted": submitted,
        "job_info": job_info,
    }


def _makeup_all_reports_for_user(user: User, report_key: str) -> tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    missing = _get_missing_report_periods_for_user(user, report_key)
    target_periods = [item.get("value") for item in missing.get("options", []) if item.get("value")]
    meta = _get_report_meta(report_key)
    task_name = meta["task_name"]
    config_data = user_to_config(user)
    if not target_periods:
        result = {
            "status": "skip",
            "message": f"暂无待补交{task_name}周期",
            "task_type": f"{task_name}补交",
            "details": {"补交周期数": 0, "成功": 0, "失败": 0},
            "items": [],
        }
        apply_execution_results_to_user(user, [result], config_data)
        return result, config_data, []

    items: List[Dict[str, Any]] = []
    latest_config_data = config_data
    delay = _report_makeup_batch_delay_seconds()
    for index, period in enumerate(target_periods):
        if index > 0 and delay > 0:
            time.sleep(delay)
        try:
            generated = _generate_report_content_for_user(user, report_key, period, generate_content=True)
            report_info = _build_report_info(
                api_client=generated["api_client"],
                config=generated["config"],
                meta=generated["meta"],
                content=generated["content"],
                target_period=period,
                submitted=generated["submitted"],
                job_info=generated["job_info"],
            )
            generated["api_client"].submit_report(report_info)
            latest_config_data = generated["config_data"]
            items.append(
                {
                    "status": "success",
                    "message": f"{period} {task_name}补交完成",
                    "task_type": f"{task_name}补交",
                    "target_period": period,
                    "title": report_info.get("title"),
                }
            )
        except Exception as e:
            items.append(
                {
                    "status": "fail",
                    "message": f"{period} {task_name}补交失败: {str(e)}",
                    "task_type": f"{task_name}补交",
                    "target_period": period,
                }
            )

    failed = [item for item in items if item.get("status") == "fail"]
    succeeded = [item for item in items if item.get("status") == "success"]
    status = "fail" if failed else ("success" if succeeded else "skip")
    if failed:
        message = f"{len(target_periods)} 个{task_name}周期未全部补交完成"
    elif succeeded:
        message = f"{len(target_periods)} 个{task_name}周期补交完成"
    else:
        message = f"{len(target_periods)} 个{task_name}周期已跳过"
    result = {
        "status": status,
        "message": message,
        "task_type": f"{task_name}补交",
        "details": {
            "补交周期数": len(target_periods),
            "成功": len(succeeded),
            "失败": len(failed),
            "请求间隔秒": delay,
        },
        "items": items,
    }
    apply_execution_results_to_user(user, [result], latest_config_data)
    return result, latest_config_data, target_periods

@router.post("/app/auth/register")
def app_register(request: Request, response: Response, req: AppRegisterRequest):
    client_ip = get_client_ip(request)
    phone = (req.phone or "").strip()
    password = (req.password or "").strip()
    tenant_id = DEFAULT_TENANT_ID
    _rate_limit_login_attempt(
        scope="app_register",
        client_ip=client_ip,
        tenant_id=tenant_id,
        principal=phone,
        ip_limit=10,
        principal_limit=3,
        per_seconds=60,
    )
    password = require_password_strength(password, label="Password")
    if not phone or len(phone) < 4:
        raise HTTPException(status_code=400, detail="请输入正确的手机号/账号")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="密码长度需为 6-100")
    if not _app_registration_enabled():
        raise HTTPException(status_code=403, detail="App registration is disabled")
    with Session(engine) as session:
        _ensure_app_registration_allowed(session, tenant_id)
        exists = session.exec(select(AppUser).where((AppUser.tenant_id == tenant_id) & (AppUser.phone == phone))).first()
        if exists:
            raise HTTPException(status_code=400, detail="该账号已注册")
        legacy = session.exec(select(User).where(_tenant_filter(User.tenant_id, tenant_id) & (User.phone == phone))).first()
        if legacy and legacy.app_password_hash:
            raise HTTPException(status_code=400, detail="该账号已注册")
        app_user = AppUser(tenant_id=tenant_id, phone=phone, password_hash=hash_password(password), enabled=True, bound_user_id=None)
        session.add(app_user)
        session.flush()
        session.add(AuditLog(tenant_id=tenant_id, actor=f"app:{app_user.id}", action="app.register", target_user_id=None, detail={}))
        session.commit()
        token = issue_token(
            subject=f"app:{app_user.id}",
            role="user",
            tenant_id=app_user.tenant_id,
            token_version=app_user.token_version,
        )
        set_auth_cookie(response, token, "user")
        return _login_payload(token, {"user_id": app_user.id, "phone": phone})

@router.post("/app/auth/login")
def app_login(request: Request, response: Response, req: AppLoginRequest):
    client_ip = get_client_ip(request)
    phone = (req.phone or "").strip()
    password = (req.password or "").strip()
    tenant_id = DEFAULT_TENANT_ID
    _rate_limit_login_attempt(
        scope="app_login",
        client_ip=client_ip,
        tenant_id=tenant_id,
        principal=phone,
        ip_limit=15,
        principal_limit=5,
        per_seconds=60,
    )
    with Session(engine) as session:
        require_active_tenant(tenant_id, session=session)
        app_user = session.exec(select(AppUser).where((AppUser.tenant_id == tenant_id) & (AppUser.phone == phone))).first()
        if app_user:
            legacy_user = session.exec(select(User).where(_tenant_filter(User.tenant_id, tenant_id) & (User.phone == phone))).first()
            if legacy_user and legacy_user.app_enabled is not True:
                _record_login_failure(
                    session,
                    tenant_id=tenant_id,
                    principal=phone,
                    client_ip=client_ip,
                    action="app.login.failed",
                    reason="account_disabled",
                    account=app_user,
                    increment=False,
                )
                raise HTTPException(status_code=403, detail="账号已被禁用")
            if app_user.enabled is not True:
                _record_login_failure(
                    session,
                    tenant_id=tenant_id,
                    principal=phone,
                    client_ip=client_ip,
                    action="app.login.failed",
                    reason="account_disabled",
                    account=app_user,
                    increment=False,
                )
                raise HTTPException(status_code=403, detail="账号已被禁用")
            _raise_if_login_locked(
                session,
                app_user,
                tenant_id=tenant_id,
                principal=phone,
                client_ip=client_ip,
                action="app.login.failed",
            )
            legacy_password_hash = _resolve_legacy_app_password_hash(legacy_user, app_user.password_hash) if legacy_user else ""
            expected_password_hash = app_user.password_hash
            if legacy_user and legacy_user.app_enabled is True and legacy_password_hash:
                expected_password_hash = legacy_password_hash
                if app_user.password_hash != legacy_password_hash or app_user.bound_user_id != legacy_user.id:
                    app_user.password_hash = legacy_password_hash
                    app_user.bound_user_id = legacy_user.id
                    app_user.phone = legacy_user.phone
                    app_user.tenant_id = legacy_user.tenant_id or tenant_id
                    app_user.enabled = bool(legacy_user.app_enabled)
                    session.add(app_user)
            if not verify_password(password, expected_password_hash):
                _record_login_failure(
                    session,
                    tenant_id=tenant_id,
                    principal=phone,
                    client_ip=client_ip,
                    action="app.login.failed",
                    reason="invalid_credentials",
                    account=app_user,
                )
                raise HTTPException(status_code=401, detail="账号或密码错误")
            _reset_login_failure_state(app_user)
            token = issue_token(
                subject=f"app:{app_user.id}",
                role="user",
                tenant_id=app_user.tenant_id,
                token_version=app_user.token_version,
            )
            session.add(app_user)
            session.add(AuditLog(tenant_id=app_user.tenant_id, actor=f"app:{app_user.id}", action="app.login", target_user_id=None, detail={}))
            session.commit()
            set_auth_cookie(response, token, "user")
            return _login_payload(token, {"user_id": app_user.id, "phone": phone})

        legacy_user = session.exec(select(User).where(_tenant_filter(User.tenant_id, tenant_id) & (User.phone == phone))).first()
        legacy_password_hash = _resolve_legacy_app_password_hash(legacy_user) if legacy_user else ""
        if not legacy_user or legacy_user.app_enabled is not True or not legacy_password_hash:
            _record_login_failure(
                session,
                tenant_id=tenant_id,
                principal=phone,
                client_ip=client_ip,
                action="app.login.failed",
                reason="invalid_credentials",
                increment=False,
            )
            raise HTTPException(status_code=401, detail="账号或密码错误")
        if not verify_password(password, legacy_password_hash):
            _record_login_failure(
                session,
                tenant_id=tenant_id,
                principal=phone,
                client_ip=client_ip,
                action="app.login.failed",
                reason="invalid_credentials",
                increment=False,
            )
            raise HTTPException(status_code=401, detail="账号或密码错误")
        app_user = AppUser(
            tenant_id=tenant_id,
            phone=phone,
            password_hash=legacy_password_hash,
            enabled=True,
            bound_user_id=legacy_user.id,
        )
        session.add(app_user)
        session.flush()
        token = issue_token(
            subject=f"app:{app_user.id}",
            role="user",
            tenant_id=app_user.tenant_id,
            token_version=app_user.token_version,
        )
        session.add(AuditLog(tenant_id=tenant_id, actor=f"app:{app_user.id}", action="app.login", target_user_id=legacy_user.id, detail={"legacy": True}))
        session.commit()
        set_auth_cookie(response, token, "user")
        return _login_payload(token, {"user_id": app_user.id, "phone": phone})


@router.post("/app/auth/logout")
def app_logout(
    response: Response,
    session: Session = Depends(get_session),
    payload: Optional[dict] = Depends(get_optional_auth_payload),
):
    if payload and payload.get("role") == "user":
        revoke_token_subject(payload, session=session)
    clear_auth_cookie(response, "user")
    return {"ok": True}

@router.get("/app/me")
def app_me(*, session: Session = Depends(get_session), payload: dict = Depends(get_user)):
    app_user = _get_authed_app_user(session=session, payload=payload)
    if not app_user.bound_user_id:
        return AppMeResponse(app_phone=app_user.phone, bound=False, task_user=None)
    try:
        user = _get_bound_task_user(session=session, app_user=app_user)
    except HTTPException:
        return AppMeResponse(app_phone=app_user.phone, bound=False, task_user=None)
    return AppMeResponse(app_phone=app_user.phone, bound=True, task_user=_sanitize_user_for_self(user))

class AppMeUpdateRequest(BaseModel):
    password: Optional[str] = None
    enable_clockin: Optional[bool] = None
    clockIn: Optional[Dict[str, Any]] = None
    reportSettings: Optional[Dict[str, Any]] = None
    pushNotifications: Optional[List[Dict[str, Any]]] = None

class AppBindRequest(BaseModel):
    task_phone: str
    task_password: str

@router.post("/app/bind")
def app_bind(
    *,
    request: Request,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
    req: AppBindRequest,
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    task_phone = (req.task_phone or "").strip()
    task_password = (req.task_password or "").strip()
    if not task_phone or len(task_phone) < 4:
        raise HTTPException(status_code=400, detail="请输入正确的工学云账号")
    if not task_password or len(task_password) < 6:
        raise HTTPException(status_code=400, detail="密码长度需为 6-100")

    client_ip = get_client_ip(request)
    _rate_limit(
        f"app_bind:{client_ip}:{app_user.id}",
        limit=10,
        per_seconds=10 * 60,
        detail="绑定尝试过于频繁，请 10 分钟后再试",
    )

    verify_on = (os.getenv("MOGUDING_BIND_VERIFY") or "1").strip().lower() not in ["0", "false", "no", "off"]
    if verify_on:
        cfg = ConfigManager(config={"config": {"user": {"phone": task_phone, "password": task_password}}})
        api_client = ApiClient(cfg)
        api_client.max_retries = 1
        try:
            api_client.login()
            token = cfg.get_value("userInfo.token")
            if not token:
                raise HTTPException(status_code=400, detail="工学云账号验证失败")
        except HTTPException:
            raise
        except Exception as e:
            msg = str(e) or "工学云账号验证失败"
            if "验证码" in msg:
                raise HTTPException(status_code=400, detail="工学云账号验证失败：触发验证码，请稍后再试")
            raise HTTPException(status_code=400, detail="工学云账号或密码错误")

    tenant_id = tenant_id_from_payload(payload)
    user = session.exec(select(User).where(_tenant_filter(User.tenant_id, tenant_id) & (User.phone == task_phone))).first()
    if user:
        other = session.exec(
            select(AppUser).where(
                (AppUser.tenant_id == tenant_id)
                & (AppUser.bound_user_id == user.id)
                & (AppUser.id != app_user.id)
            )
        ).first()
        if other:
            raise HTTPException(status_code=400, detail="该工学云账号已被其他账号绑定")
        user.password = encrypt_secret(task_password)
        if user.enable_clockin is None:
            user.enable_clockin = True
    else:
        user = User(
            tenant_id=tenant_id,
            phone=task_phone,
            password=encrypt_secret(task_password),
            remark=None,
            app_enabled=True,
            enable_clockin=True,
        )
        _ensure_clockin_schedule_defaults(user)
        session.add(user)
        session.flush()

    app_user.bound_user_id = user.id
    if verify_on:
        sync_runtime_fields_to_user(user, cfg.config)
    session.add(app_user)
    session.add(user)
    session.add(AuditLog(tenant_id=tenant_id, actor=str(payload.get("sub")), action="app.bind", target_user_id=user.id, detail={"task_phone": _mask_phone(task_phone)}))
    session.commit()
    session.refresh(user)
    remove_user_job(user.id)
    if user.enable_clockin or _any_report_enabled(user):
        add_user_job(user)
    return {"ok": True, "user_id": user.id}

@router.get("/app/account-address")
def app_account_address(
    *,
    request: Request,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    client_ip = get_client_ip(request)
    _rate_limit(f"app_account_addr:{client_ip}:{user.id}", limit=3, per_seconds=60)
    
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法自动获取账号地址")

    try:
        config_data = user_to_config(user)
        config = ConfigManager(config=config_data)
        api_client = ApiClient(config)
        _ensure_remote_runtime(api_client, config)
        checkin = api_client.get_checkin_info() or {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "获取账号地址失败")

    candidates: List[str] = []
    for k in [
        "address",
        "detailAddress",
        "lastDetailAddress",
        "lastAddress",
        "practiceAddress",
        "practiceDetailAddress",
        "companyAddress",
        "workAddress",
    ]:
        v = checkin.get(k)
        if isinstance(v, str):
            s = v.strip()
            if s and s not in candidates:
                candidates.append(s)

    best = candidates[0] if candidates else None
    if candidates:
        best = sorted(candidates, key=lambda x: len(x), reverse=True)[0]
    if not best:
        raise HTTPException(status_code=404, detail="未获取到账号地址（可能该账号暂无打卡记录）")

    try:
        clock_in = user.clockIn if isinstance(user.clockIn, dict) else {}
        loc = clock_in.get("location") if isinstance(clock_in.get("location"), dict) else {}
        loc2 = dict(loc)
        loc2["address"] = best
        parts = [p.strip() for p in re.split(r"\s*[·,/，,]\s*", str(best)) if p and p.strip()]
        if len(parts) >= 1 and not loc2.get("province"):
            loc2["province"] = parts[0]
        if len(parts) >= 2 and not loc2.get("city"):
            loc2["city"] = parts[1]
        if len(parts) >= 3 and not loc2.get("area"):
            loc2["area"] = parts[2]
        clock_in2 = dict(clock_in)
        clock_in2["location"] = loc2
        user.clockIn = clock_in2
        session.add(user)
        session.commit()
    except Exception:
        session.rollback()

    return {
        "ok": True,
        "address": best,
        "addressCandidates": candidates,
        "checkinTime": checkin.get("attendenceTime"),
        "type": checkin.get("type"),
    }

@router.patch("/app/me")
def app_update_me(
    *,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
    req: AppMeUpdateRequest,
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    changed: List[str] = []
    if req.password is not None:
        pw = (req.password or "").strip()
        if not pw or len(pw) < 6 or len(pw) > 100:
            raise HTTPException(status_code=400, detail="密码长度需为 6-100")
        user.password = encrypt_secret(pw)
        changed.append("password")
    if req.enable_clockin is not None:
        user.enable_clockin = bool(req.enable_clockin)
        changed.append("enable_clockin")
    if req.clockIn is not None:
        if not isinstance(req.clockIn, dict):
            raise HTTPException(status_code=400, detail="clockIn 格式错误")
        user.clockIn = req.clockIn
        changed.append("clockIn")
        _ensure_clockin_schedule_defaults(user)
    if req.reportSettings is not None:
        if not isinstance(req.reportSettings, dict):
            raise HTTPException(status_code=400, detail="reportSettings 格式错误")
        user.reportSettings = req.reportSettings
        changed.append("reportSettings")
    if req.pushNotifications is not None:
        if not isinstance(req.pushNotifications, list):
            raise HTTPException(status_code=400, detail="pushNotifications 格式错误")
        user.pushNotifications = normalize_push_notifications(req.pushNotifications, user.pushNotifications)
        changed.append("pushNotifications")
    session.add(user)
    session.add(AuditLog(tenant_id=user.tenant_id, actor=str(payload.get("sub")), action="app.user.update", target_user_id=user.id, detail={"fields": changed}))
    session.commit()
    session.refresh(user)
    remove_user_job(user.id)
    if user.enable_clockin or _any_report_enabled(user):
        add_user_job(user)
    return _sanitize_user_for_self(user)

@router.post("/app/run")
def app_run(
    *,
    request: Request,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
    req: Optional[AppRunRequest] = None,
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    client_ip = get_client_ip(request)
    if _should_rate_limit_run_request(req):
        _rate_limit(f"app_run:{client_ip}:{user.id}", limit=3, per_seconds=60)
    config_data = user_to_config(user)
    
    specific_task_type = req.task_type if req else None
    results = run_task_by_config(
        config_data,
        specific_task_type=specific_task_type,
        force_report=bool(req.force_report) if req else False,
        target_period=req.target_period if req else None,
        preauthorization_hooks=build_preauthorization_hooks(user, db_engine=engine),
    )
    status = apply_execution_results_to_user(user, results, config_data)
    session.add(AuditLog(tenant_id=user.tenant_id, actor=str(payload.get("sub")), action="app.user.run", target_user_id=user.id, detail={"status": status}))
    session.add(user)
    session.commit()
    return {"results": results}


@router.post("/app/clock-in/alipay/continue")
def app_continue_alipay_clockin(
    *,
    request: Request,
    req: AlipayClockInContinueRequest,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    out_register_no, target_type = _alipay_continue_values(req)
    client_ip = get_client_ip(request)
    _rate_limit(f"app_run:{client_ip}:{user.id}", limit=3, per_seconds=60)

    result, _ = _continue_alipay_clockin_for_user(
        user,
        out_register_no,
        target_type,
    )
    session.add(
        AuditLog(
            tenant_id=user.tenant_id,
            actor=str(payload.get("sub")),
            action="app.clockin.alipay.continue",
            target_user_id=user.id,
            detail={"status": result.get("status"), "target_type": target_type},
        )
    )
    session.add(user)
    session.commit()
    return {"result": result}


@router.get(
    "/app/clock-in/preauthorizations",
    response_model=ClockInPreauthorizationListResponse,
)
def app_clockin_preauthorizations(
    *,
    request: Request,
    scope: str = Query("future"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    target_type: Optional[str] = Query(None),
    refresh_plan: bool = Query(False),
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    client_ip = get_client_ip(request)
    _rate_limit(f"app_preauthorization_list:{client_ip}:{user.id}", limit=30, per_seconds=60)
    try:
        return _list_clockin_preauthorizations_for_user(
            session,
            user,
            scope=scope,
            page=page,
            page_size=page_size,
            status=status,
            target_type=target_type,
            refresh_plan=refresh_plan,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/app/clock-in/preauthorizations/start",
    response_model=ClockInPreauthorizationStartResponse,
)
def app_start_clockin_preauthorization(
    *,
    request: Request,
    req: ClockInPreauthorizationStartRequest,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    client_ip = get_client_ip(request)
    _rate_limit(
        f"app_preauthorization_start:{client_ip}:{user.id}",
        limit=CLOCKIN_PREAUTHORIZATION_ACTION_LIMIT,
        per_seconds=60,
    )
    try:
        result = _start_clockin_preauthorization_for_user(user, req, session=session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    target_date, target_type = _preauthorization_target_values(req)
    session.add(user)
    session.add(
        AuditLog(
            tenant_id=user.tenant_id,
            actor=str(payload.get("sub")),
            action="app.clockin.preauthorization.start",
            target_user_id=user.id,
            detail={
                "target_date": target_date.isoformat(),
                "target_type": target_type,
                "status": "started",
            },
        )
    )
    session.commit()
    return result


@router.post(
    "/app/clock-in/preauthorizations/complete",
    response_model=ClockInPreauthorizationCompleteResponse,
)
def app_complete_clockin_preauthorization(
    *,
    request: Request,
    req: ClockInPreauthorizationCompleteRequest,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    client_ip = get_client_ip(request)
    _rate_limit(
        f"app_preauthorization_complete:{client_ip}:{user.id}",
        limit=CLOCKIN_PREAUTHORIZATION_ACTION_LIMIT,
        per_seconds=60,
    )
    try:
        result = _complete_clockin_preauthorization_for_user(
            session,
            user,
            req.registration_ticket,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session.add(
        AuditLog(
            tenant_id=user.tenant_id,
            actor=str(payload.get("sub")),
            action="app.clockin.preauthorization.complete",
            target_user_id=user.id,
            detail={
                "target_date": result["target_date"],
                "target_type": result["target_type"],
                "status": result["status"],
            },
        )
    )
    session.commit()
    return {"item": result}


@router.get("/app/execution")
def app_execution(*, session: Session = Depends(get_session), payload: dict = Depends(get_user)):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    return {"results": user.last_execution_result or []}


@router.get("/app/clock-in/missing-days")
def app_clockin_missing_days(
    *,
    request: Request,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    client_ip = get_client_ip(request)
    _rate_limit(f"app_clockin_missing:{client_ip}:{user.id}", limit=10, per_seconds=60)
    try:
        return _get_missing_clockin_days_for_user(user)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "获取缺卡日期失败")


@router.post("/app/clock-in/makeup")
def app_clockin_makeup(
    *,
    request: Request,
    req: ClockInMakeupRequest,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    client_ip = get_client_ip(request)
    idempotency_key = _normalize_idempotency_key(
        request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    )
    tenant_id = tenant_id_from_payload(payload)
    request_hash = ""
    try:
        target_dates = _clockin_makeup_dates_from_request(req)
        target_type = _clockin_makeup_type_from_request(req)
        tenant_id = tenant_id_from_payload(payload)
        request_hash = _operation_request_hash(
            {
                "actor": str(payload.get("sub") or ""),
                "operation": "app.clockin.makeup",
                "target_dates": target_dates,
                "target_type": target_type,
                "tenant_id": tenant_id,
                "user_id": int(user.id),
            }
        )
        if idempotency_key:
            existing_response = _manual_operation_existing_response(
                session,
                tenant_id=tenant_id,
                operation="app.clockin.makeup",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if existing_response is not None:
                return existing_response
        _rate_limit(f"app_clockin_makeup:{client_ip}:{user.id}", limit=3, per_seconds=60)
        if idempotency_key:
            claimed_response = claim_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("app.clockin.makeup", idempotency_key),
                request_hash=request_hash,
                conflict_detail="Idempotency key already used with different request parameters",
                pending_detail="Request is already being processed",
            )
            if claimed_response is not None:
                return claimed_response
        result, config_data = _makeup_clockin_for_user(user, target_dates, target_type)
        sync_runtime_fields_to_user(user, config_data)
        response_payload = {"ok": result.get("status") != "fail", "result": result}
        if idempotency_key:
            finalize_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("app.clockin.makeup", idempotency_key),
                request_hash=request_hash,
                response=response_payload,
            )
        session.add(AuditLog(tenant_id=user.tenant_id, actor=str(payload.get("sub")), action="app.clockin.makeup", target_user_id=user.id, detail={"target_dates": target_dates, "target_type": target_type, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return response_payload
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        if idempotency_key:
            try:
                finalize_idempotency_record(
                    db_engine=engine,
                    tenant_id=tenant_id,
                    logical_key=_manual_operation_idempotency_storage_key("app.clockin.makeup", idempotency_key),
                    request_hash=request_hash,
                    status=IDEMPOTENCY_STATUS_FAILED,
                    error=str(e),
                )
            except Exception:
                logger.debug("failed to finalize idempotency state", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e) or "补卡失败")


@router.post("/app/clock-in/makeup-all")
def app_clockin_makeup_all(
    *,
    request: Request,
    req: Optional[ClockInMakeupRequest] = None,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    client_ip = get_client_ip(request)
    idempotency_key = _normalize_idempotency_key(
        request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    )
    tenant_id = tenant_id_from_payload(payload)
    request_hash = ""
    try:
        target_type = _clockin_makeup_type_from_request(req or ClockInMakeupRequest())
        tenant_id = tenant_id_from_payload(payload)
        request_hash = _operation_request_hash(
            {
                "actor": str(payload.get("sub") or ""),
                "operation": "app.clockin.makeup_all",
                "target_type": target_type,
                "tenant_id": tenant_id,
                "user_id": int(user.id),
            }
        )
        if idempotency_key:
            existing_response = _manual_operation_existing_response(
                session,
                tenant_id=tenant_id,
                operation="app.clockin.makeup_all",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if existing_response is not None:
                return existing_response
        _rate_limit(f"app_clockin_makeup_all:{client_ip}:{user.id}", limit=2, per_seconds=60)
        if idempotency_key:
            claimed_response = claim_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("app.clockin.makeup_all", idempotency_key),
                request_hash=request_hash,
                conflict_detail="Idempotency key already used with different request parameters",
                pending_detail="Request is already being processed",
            )
            if claimed_response is not None:
                return claimed_response
        result, config_data, target_dates = _makeup_all_missing_clockin_for_user(user, target_type)
        sync_runtime_fields_to_user(user, config_data)
        response_payload = {"ok": result.get("status") != "fail", "result": result, "target_dates": target_dates, "target_type": target_type}
        if idempotency_key:
            finalize_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("app.clockin.makeup_all", idempotency_key),
                request_hash=request_hash,
                response=response_payload,
            )
        session.add(AuditLog(tenant_id=user.tenant_id, actor=str(payload.get("sub")), action="app.clockin.makeup_all", target_user_id=user.id, detail={"target_dates": target_dates, "target_type": target_type, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return response_payload
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        if idempotency_key:
            try:
                finalize_idempotency_record(
                    db_engine=engine,
                    tenant_id=tenant_id,
                    logical_key=_manual_operation_idempotency_storage_key("app.clockin.makeup_all", idempotency_key),
                    request_hash=request_hash,
                    status=IDEMPOTENCY_STATUS_FAILED,
                    error=str(e),
                )
            except Exception:
                logger.debug("failed to finalize idempotency state", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e) or "全部补卡失败")


@router.get("/app/reports/{report_key}/missing-periods")
def app_report_missing_periods(
    *,
    report_key: str,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    try:
        return _get_missing_report_periods_for_user(user, report_key)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "获取未提交周期失败")


@router.post("/app/reports/{report_key}/generate")
def app_generate_report(
    *,
    report_key: str,
    target_period: Optional[str] = None,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    try:
        generated = _generate_report_content_for_user(user, report_key, target_period)
        sync_runtime_fields_to_user(user, generated["config_data"])
        session.add(
            AuditLog(
                tenant_id=user.tenant_id,
                actor=str(payload.get("sub")),
                action="app.report.generate",
                target_user_id=user.id,
                detail=_ai_audit_detail(
                    generated["config"],
                    generated["title"],
                    {"report_key": report_key, "target_period": target_period},
                ),
            )
        )
        session.add(user)
        session.commit()
        return {"ok": True, "title": generated["title"], "content": generated["content"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "生成报告失败")


@router.post("/app/reports/{report_key}/submit")
def app_submit_report(
    *,
    report_key: str,
    req: AppReportSubmitRequest,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="报告内容不能为空")
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    try:
        generated = _generate_report_content_for_user(user, report_key, req.target_period, generate_content=False)
        report_info = _build_report_info(
            api_client=generated["api_client"],
            config=generated["config"],
            meta=generated["meta"],
            content=content,
            target_period=req.target_period,
            submitted=generated["submitted"],
            job_info=generated["job_info"],
        )
        generated["api_client"].submit_report(report_info)
        sync_runtime_fields_to_user(user, generated["config_data"])
        session.add(user)
        session.commit()
        return {"ok": True, "title": report_info["title"], "submitted_at": report_info["reportTime"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "提交报告失败")


@router.post("/app/reports/{report_key}/makeup-all")
def app_makeup_all_reports(
    *,
    report_key: str,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    try:
        result, config_data, target_periods = _makeup_all_reports_for_user(user, report_key)
        sync_runtime_fields_to_user(user, config_data)
        session.add(AuditLog(tenant_id=user.tenant_id, actor=str(payload.get("sub")), action="app.report.makeup_all", target_user_id=user.id, detail={"report_key": report_key, "target_periods": target_periods, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return {"ok": result.get("status") != "fail", "result": result, "target_periods": target_periods, "report_key": report_key}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e) or "全部补交报告失败")

@router.post("/app/reports/daily/generate")
def app_generate_daily_report(
    *,
    request: Request,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    try:
        generated = _generate_report_content_for_user(user, "daily", None)
        submitted = generated["submitted"]
        data = submitted.get("data", []) if isinstance(submitted, dict) else []
        already_submitted = False
        current_time = datetime.datetime.now()
        if isinstance(data, list) and data:
            last = data[0]
            ts = last.get("createTime")
            if isinstance(ts, str):
                try:
                    last_time = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    if last_time.date() == current_time.date():
                        already_submitted = True
                except (TypeError, ValueError):
                    logger.debug("ignore invalid submitted daily report createTime: %r", ts)

        sync_runtime_fields_to_user(user, generated["config_data"])
        session.add(user)
        session.add(
            AuditLog(
                tenant_id=user.tenant_id,
                actor=str(payload.get("sub")),
                action="app.report.daily.generate",
                target_user_id=user.id,
                detail=_ai_audit_detail(generated["config"], generated["title"], {"already_submitted": already_submitted}),
            )
        )
        session.commit()
        return {"ok": True, "title": generated["title"], "content": generated["content"], "already_submitted": already_submitted}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "生成日报失败")

@router.post("/app/reports/daily/submit")
def app_submit_daily_report(
    *,
    request: Request,
    session: Session = Depends(get_session),
    payload: dict = Depends(get_user),
    req: AppReportSubmitRequest,
):
    app_user = _get_authed_app_user(session=session, payload=payload)
    user = _get_bound_task_user(session=session, app_user=app_user)
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="日报内容不能为空")

    try:
        generated = _generate_report_content_for_user(user, "daily", None, generate_content=False)
        submitted = generated["submitted"]
        data = submitted.get("data", []) if isinstance(submitted, dict) else []
        current_time = datetime.datetime.now()
        if isinstance(data, list) and data:
            last = data[0]
            ts = last.get("createTime")
            if isinstance(ts, str):
                try:
                    last_time = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    if last_time.date() == current_time.date():
                        raise HTTPException(status_code=400, detail="今天已经提交过日报")
                except HTTPException:
                    raise
                except (TypeError, ValueError):
                    logger.debug("ignore invalid submitted daily report createTime: %r", ts)

        title = generated["title"]
        report_info = {
            "title": title,
            "content": content,
            "attachments": "",
            "reportType": "day",
            "jobId": generated["job_info"].get("jobId", None),
            "reportTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "formFieldDtoList": generated["api_client"].get_from_info(7),
        }
        generated["api_client"].submit_report(report_info)
        sync_runtime_fields_to_user(user, generated["config_data"])
        session.add(user)
        session.add(AuditLog(tenant_id=user.tenant_id, actor=str(payload.get("sub")), action="app.report.daily.submit", target_user_id=user.id, detail={"title": title}))
        session.commit()
        return {"ok": True, "title": title, "submitted_at": report_info["reportTime"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "提交日报失败")

def _mask_phone(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 4:
        last4 = digits[-4:]
        return "********" + last4
    return "*" * max(1, len(digits))

def _mask_number_like(value: Any) -> Any:
    if value is None:
        return value
    s = str(value).strip()
    if not s:
        return value
    sign = ""
    if s[0] in ["+", "-"]:
        sign = s[0]
        s = s[1:]
    digits = [c for c in s if c.isdigit()]
    if not digits:
        return value
    first = digits[0]
    masked_len = max(1, len(digits) - 1)
    return f"{sign}{first}{'*' * masked_len}"

def _mask_address(value: Any) -> Any:
    if value is None:
        return value
    s = str(value).strip()
    if not s:
        return value
    parts = [p.strip() for p in re.split(r"\s*[·,/，,]\s*", s) if p and p.strip()]
    if len(parts) >= 2:
        return f"{parts[0]}·{parts[1]}·***"
    m = re.search(r"(省|自治区|特别行政区)", s)
    if m:
        prov_end = m.end()
        rest = s[prov_end:]
        m2 = re.search(r"(市|州|盟|地区)", rest)
        if m2:
            city_end = prov_end + m2.end()
            return f"{s[:city_end]}·***"
        return f"{s[:prov_end]}·***"
    m3 = re.search(r"市", s)
    if m3:
        return f"{s[:m3.end()]}·***"
    return "***"

def _mask_clockin(clock_in: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(clock_in)
    location = out.get("location")
    if isinstance(location, dict):
        loc = dict(location)
        if "address" in loc:
            loc["address"] = _mask_address(loc.get("address"))
        if "latitude" in loc:
            loc["latitude"] = _mask_number_like(loc.get("latitude"))
        if "longitude" in loc:
            loc["longitude"] = _mask_number_like(loc.get("longitude"))
        if "area" in loc and loc.get("area"):
            loc["area"] = "***"
        out["location"] = loc
    return out

class AiTestRequest(BaseModel):
    apiUrl: str
    apikey: str
    model: str

class ReportSubmitRequest(BaseModel):
    content: str
    target_period: Optional[str] = None

_GEOCODE_CACHE: "OrderedDict[tuple, tuple[float, Any]]" = OrderedDict()
_GEOCODE_LOCK = threading.Lock()

def _geocode_cache_get(key: tuple) -> Any:
    now = time.time()
    with _GEOCODE_LOCK:
        hit = _GEOCODE_CACHE.get(key)
        if not hit:
            return None
        exp, value = hit
        if exp <= now:
            _GEOCODE_CACHE.pop(key, None)
            return None
        _GEOCODE_CACHE.move_to_end(key)
        return value

def _geocode_cache_set(key: tuple, value: Any, ttl_seconds: int = 3600, maxsize: int = 800) -> None:
    now = time.time()
    exp = now + int(ttl_seconds)
    with _GEOCODE_LOCK:
        _GEOCODE_CACHE[key] = (exp, value)
        _GEOCODE_CACHE.move_to_end(key)
        while len(_GEOCODE_CACHE) > int(maxsize):
            _GEOCODE_CACHE.popitem(last=False)

def _geocode_service_config(provider_env: str, default_provider: str) -> tuple[str, str, str]:
    provider = (os.getenv(provider_env) or "").strip().lower() or default_provider
    amap_key = (os.getenv("AMAP_KEY") or "").strip()
    baidu_key = (os.getenv("BAIDU_MAP_AK") or os.getenv("BAIDU_MAP_KEY") or "").strip()
    if provider in {"mapchaxun", "mcx"}:
        return "mapchaxun", amap_key, baidu_key
    if provider in {"baidu", "bd"}:
        if not baidu_key:
            raise HTTPException(status_code=400, detail="已选择百度地理编码，请配置 BAIDU_MAP_AK")
        return "baidu", amap_key, baidu_key
    if provider in {"amap", "gaode"}:
        if not amap_key:
            raise HTTPException(status_code=400, detail="已选择高德地理编码，请配置 AMAP_KEY")
        return "amap", amap_key, baidu_key
    if provider in {"osm", "nominatim", "openstreetmap"}:
        return "osm", amap_key, baidu_key
    raise HTTPException(status_code=400, detail=f"不支持的地理编码服务: {provider}")

def _geocode_search_provider_config() -> tuple[str, str, str]:
    return _geocode_service_config("GEOCODE_SEARCH_PROVIDER", "mapchaxun")

def _geocode_provider_config() -> tuple[str, str, str]:
    amap_key = (os.getenv("AMAP_KEY") or "").strip()
    baidu_key = (os.getenv("BAIDU_MAP_AK") or os.getenv("BAIDU_MAP_KEY") or "").strip()
    default_provider = "baidu" if baidu_key else ("amap" if amap_key else "osm")
    return _geocode_service_config("GEOCODE_PROVIDER", default_provider)

def _baidu_coord_types() -> tuple[str, str]:
    default = (os.getenv("BAIDU_MAP_COORD_TYPE") or "gcj02ll").strip() or "gcj02ll"
    input_type = (os.getenv("BAIDU_MAP_INPUT_COORD_TYPE") or default).strip() or default
    output_type = (
        os.getenv("BAIDU_MAP_OUTPUT_COORD_TYPE")
        or os.getenv("BAIDU_MAP_RETURN_COORD_TYPE")
        or default
    ).strip() or default
    return input_type, output_type

def _mapchaxun_location(data: Dict[str, Any]) -> tuple[float, float]:
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    loc = result.get("location") if isinstance(result.get("location"), dict) else {}
    if loc:
        return float(loc.get("lng")), float(loc.get("lat"))
    loc_list = data.get("location")
    if isinstance(loc_list, list) and len(loc_list) >= 2:
        return float(loc_list[0]), float(loc_list[1])
    loc_val = data.get("locationVal")
    if isinstance(loc_val, str) and "," in loc_val:
        lng_s, lat_s = loc_val.split(",", 1)
        return float(lng_s), float(lat_s)
    raise ValueError("mapchaxun 未返回有效经纬度")

def _mapchaxun_address(data: Dict[str, Any]) -> Dict[str, Any]:
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    result_addr = result.get("address_components") if isinstance(result.get("address_components"), dict) else {}
    top_addr = data.get("address_components") if isinstance(data.get("address_components"), dict) else {}
    address = {**result_addr, **top_addr}
    if "district" in address and "county" not in address:
        address["county"] = address.get("district")
    return address

def _rate_limit(key: str, limit: int, per_seconds: int, detail: Optional[str] = None) -> None:
    check_rate_limit(key, limit, per_seconds, detail)


def _rate_limit_principal_digest(tenant_id: str, principal: str) -> str:
    normalized = f"{tenant_id}:{principal}".strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _rate_limit_login_attempt(
    *,
    scope: str,
    client_ip: str,
    tenant_id: str,
    principal: str,
    ip_limit: int,
    principal_limit: int,
    per_seconds: int,
) -> None:
    _rate_limit(f"{scope}:ip:{client_ip}", limit=ip_limit, per_seconds=per_seconds)
    principal_key = _rate_limit_principal_digest(tenant_id or DEFAULT_TENANT_ID, principal or "")
    _rate_limit(
        f"{scope}:principal:{tenant_id or DEFAULT_TENANT_ID}:{principal_key}",
        limit=principal_limit,
        per_seconds=per_seconds,
    )


def _login_lockout_failures() -> int:
    return int_env("LOGIN_LOCKOUT_FAILURES", 5, min_value=2, max_value=50)


def _login_lockout_seconds() -> int:
    return int_env("LOGIN_LOCKOUT_SECONDS", 15 * 60, min_value=60, max_value=24 * 60 * 60)


def _as_aware_utc(value: Any) -> datetime.datetime | None:
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _login_locked_until(account: Any) -> datetime.datetime | None:
    locked_until = _as_aware_utc(getattr(account, "locked_until", None))
    if locked_until and locked_until > utc_now():
        return locked_until
    return None


def _reset_login_failure_state(account: Any) -> None:
    if account is None:
        return
    if hasattr(account, "failed_login_count"):
        account.failed_login_count = 0
    if hasattr(account, "locked_until"):
        account.locked_until = None


def _login_failure_detail(*, tenant_id: str, principal: str, client_ip: str, reason: str, account: Any = None) -> Dict[str, Any]:
    detail = {
        "principal_hash": _rate_limit_principal_digest(tenant_id or DEFAULT_TENANT_ID, principal or ""),
        "client_ip": client_ip,
        "reason": reason,
    }
    if account is not None and hasattr(account, "failed_login_count"):
        detail["failed_login_count"] = int(getattr(account, "failed_login_count", 0) or 0)
    locked_until = _login_locked_until(account)
    if locked_until is not None:
        detail["locked_until"] = locked_until.isoformat()
    return detail


def _record_login_failure(
    session: Session,
    *,
    tenant_id: str,
    principal: str,
    client_ip: str,
    action: str,
    reason: str,
    account: Any = None,
    increment: bool = True,
) -> None:
    if account is not None and increment and hasattr(account, "failed_login_count"):
        account.failed_login_count = int(getattr(account, "failed_login_count", 0) or 0) + 1
        if int(account.failed_login_count or 0) >= _login_lockout_failures() and hasattr(account, "locked_until"):
            account.locked_until = utc_now() + datetime.timedelta(seconds=_login_lockout_seconds())
        session.add(account)
    session.add(
        AuditLog(
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            actor="auth:failed",
            action=action,
            target_user_id=None,
            detail=_login_failure_detail(
                tenant_id=tenant_id,
                principal=principal,
                client_ip=client_ip,
                reason=reason,
                account=account,
            ),
        )
    )
    session.commit()


def _raise_if_login_locked(session: Session, account: Any, *, tenant_id: str, principal: str, client_ip: str, action: str) -> None:
    if _login_locked_until(account) is None:
        return
    _record_login_failure(
        session,
        tenant_id=tenant_id,
        principal=principal,
        client_ip=client_ip,
        action=action,
        reason="account_locked",
        account=account,
        increment=False,
    )
    raise HTTPException(status_code=423, detail="Account is temporarily locked")


def _ensure_clockin_schedule_defaults(user: User):
    if not isinstance(user.clockIn, dict):
        return
    schedule = user.clockIn.get("schedule")
    if not isinstance(schedule, dict):
        schedule = {}
        user.clockIn["schedule"] = schedule
    schedule.setdefault("startTime", "07:30")
    schedule.setdefault("endTime", "18:00")
    weekdays = schedule.get("weekdays")
    if not isinstance(weekdays, list) or len(weekdays) == 0:
        custom_days = user.clockIn.get("customDays")
        if isinstance(custom_days, list) and len(custom_days) > 0:
            schedule["weekdays"] = custom_days
        else:
            schedule["weekdays"] = [1, 2, 3, 4, 5, 6, 7]
    if not schedule.get("totalDays"):
        schedule["totalDays"] = 180
    if schedule.get("totalDays") and not schedule.get("startDate"):
        schedule["startDate"] = datetime.date.today().strftime("%Y-%m-%d")

class LoginRequest(BaseModel):
    username: str
    password: str


class NotificationSettingsUpdateRequest(BaseModel):
    smtp: Dict[str, Any]


class NotificationSettingsTestRequest(BaseModel):
    smtp: Dict[str, Any]


class ProxySettingsUpdateRequest(BaseModel):
    proxy: Dict[str, Any]


class AiSettingsUpdateRequest(BaseModel):
    ai: Dict[str, Any]


class AiSettingsTestRequest(BaseModel):
    ai: Optional[Dict[str, Any]] = None

@router.post("/auth/login")
def admin_login(request: Request, response: Response, req: LoginRequest):
    client_ip = get_client_ip(request)
    username = (req.username or "").strip()
    password = (req.password or "").strip()
    tenant_id = DEFAULT_TENANT_ID
    _rate_limit_login_attempt(
        scope="admin_login",
        client_ip=client_ip,
        tenant_id=tenant_id,
        principal=username,
        ip_limit=10,
        principal_limit=5,
        per_seconds=60,
    )
    with Session(engine) as session:
        require_active_tenant(tenant_id, session=session)
        user = session.exec(
            select(AdminUser).where((AdminUser.tenant_id == tenant_id) & (AdminUser.username == username))
        ).first()
        if not user or not user.enabled:
            _record_login_failure(
                session,
                tenant_id=tenant_id,
                principal=username,
                client_ip=client_ip,
                action="auth.login.failed",
                reason="invalid_credentials",
                account=user if user is not None else None,
                increment=False,
            )
            raise HTTPException(status_code=401, detail="账号或密码错误")
        _raise_if_login_locked(
            session,
            user,
            tenant_id=tenant_id,
            principal=username,
            client_ip=client_ip,
            action="auth.login.failed",
        )
        if not verify_password(password, user.password_hash):
            _record_login_failure(
                session,
                tenant_id=tenant_id,
                principal=username,
                client_ip=client_ip,
                action="auth.login.failed",
                reason="invalid_credentials",
                account=user,
            )
            raise HTTPException(status_code=401, detail="账号或密码错误")
        _reset_login_failure_state(user)
        role = user.role or "viewer"
        token = issue_token(subject=username, role=role, tenant_id=user.tenant_id, token_version=user.token_version)
        session.add(user)
        session.add(AuditLog(tenant_id=user.tenant_id, actor=username, action="auth.login", target_user_id=None, detail={"role": role}))
        session.commit()
        set_auth_cookie(response, token, role)
        return _login_payload(token, {"role": role, "username": username})


@router.get("/auth/me")
def admin_me(payload: dict = Depends(require_audit_read)):
    role = str(payload.get("role") or "")
    return {
        "username": str(payload.get("sub") or ""),
        "role": role,
        "permissions": sorted(permissions_for_payload(payload)),
    }


@router.post("/auth/logout")
def admin_logout(
    response: Response,
    session: Session = Depends(get_session),
    payload: Optional[dict] = Depends(get_optional_auth_payload),
):
    if payload and payload.get("role") != "user":
        revoke_token_subject(payload, session=session)
    clear_auth_cookie(response, "admin")
    return {"ok": True}

class AuditLogPageResponse(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    page: int
    pageSize: int

@router.get("/audit-logs/page", response_model=AuditLogPageResponse)
def read_audit_logs_page(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_audit_read),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    q: Optional[str] = Query(None, max_length=60),
):
    tenant_id = tenant_id_from_payload(admin)
    stmt = select(AuditLog).where(_tenant_filter(AuditLog.tenant_id, tenant_id))
    if q:
        qq = q.strip()
        stmt = stmt.where((AuditLog.actor.contains(qq)) | (AuditLog.action.contains(qq)))
    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    rows = session.exec(
        stmt.order_by(AuditLog.id.desc()).offset((page - 1) * pageSize).limit(pageSize)
    ).all()
    items = [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
            "actor": r.actor,
            "action": r.action,
            "target_user_id": r.target_user_id,
            "detail": r.detail,
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "pageSize": pageSize}


@router.delete("/audit-logs")
def clear_audit_logs(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_audit_purge),
):
    if not env_flag("ALLOW_AUDIT_LOG_PURGE"):
        raise HTTPException(status_code=403, detail="Audit log purge is disabled")
    tenant_id = tenant_id_from_payload(admin)
    rows = session.exec(select(AuditLog).where(_tenant_filter(AuditLog.tenant_id, tenant_id))).all()
    deleted = len(rows)
    for row in rows:
        session.delete(row)
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=str(admin.get("sub") or "admin"),
            action="audit.purge",
            target_user_id=None,
            detail={"deleted": deleted},
        )
    )
    session.commit()
    return {"ok": True, "deleted": deleted}


@router.get("/settings/notifications")
def get_notification_settings(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_settings_read),
):
    return _sanitize_notification_settings_for_read(_get_notification_settings(session, tenant_id_from_payload(admin)))


@router.patch("/settings/notifications")
def update_notification_settings(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_settings_manage),
    req: NotificationSettingsUpdateRequest,
):
    tenant_id = tenant_id_from_payload(admin)
    settings = _save_notification_settings(session, req.smtp, tenant_id)
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=admin.get("sub"),
            action="settings.notifications.update",
            target_user_id=None,
            detail={"fields": ["smtp"]},
        )
    )
    session.commit()
    return _sanitize_notification_settings_for_read(settings)


@router.post("/settings/notifications/smtp/test")
def test_notification_smtp(
    *,
    admin: dict = Depends(require_settings_manage),
    req: NotificationSettingsTestRequest,
):
    smtp = normalize_smtp_settings(req.smtp)
    if not str(smtp.get("username") or "").strip():
        raise HTTPException(status_code=400, detail="请填写 QQ 邮箱")
    if not str(smtp.get("password") or "").strip():
        raise HTTPException(status_code=400, detail="请填写授权码")
    if not str(smtp.get("from") or "").strip():
        raise HTTPException(status_code=400, detail="请填写发件人名称")
    to = send_test_smtp_message(smtp)
    return {"ok": True, "to": to}


@router.get("/settings/proxy")
def get_proxy_settings(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_settings_read),
):
    return {"proxy": load_global_proxy_settings(tenant_id=tenant_id_from_payload(admin))}


@router.patch("/settings/proxy")
def update_proxy_settings(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_settings_manage),
    req: ProxySettingsUpdateRequest,
):
    tenant_id = tenant_id_from_payload(admin)
    proxy = _save_proxy_settings(session, req.proxy, tenant_id)
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=admin.get("sub"),
            action="settings.proxy.update",
            target_user_id=None,
            detail={"enabled": bool(proxy.get("enabled"))},
        )
    )
    session.commit()
    return {"proxy": proxy}


@router.get("/settings/ai")
def get_ai_settings(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_settings_read),
):
    ai_settings = load_global_ai_settings(session, tenant_id_from_payload(admin))
    return {"ai": sanitize_ai_settings_for_read(ai_settings)}


@router.patch("/settings/ai")
def update_ai_settings(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_settings_manage),
    req: AiSettingsUpdateRequest,
):
    tenant_id = tenant_id_from_payload(admin)
    ai_settings = save_global_ai_settings(session, req.ai, tenant_id)
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=admin.get("sub"),
            action="settings.ai.update",
            target_user_id=None,
            detail={
                "endpoint": _ai_endpoint_detail(str(ai_settings.get("apiUrl") or "")),
                "model": str(ai_settings.get("model") or ""),
            },
        )
    )
    session.commit()
    return {"ai": sanitize_ai_settings_for_read(ai_settings)}


@router.post("/settings/ai/test")
def test_ai_settings(
    *,
    request: Request,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_settings_manage),
    req: AiSettingsTestRequest,
):
    tenant_id = tenant_id_from_payload(admin)
    current = load_global_ai_settings(session, tenant_id)
    payload = req.ai if isinstance(req.ai, dict) else current
    ai_settings = validate_ai_settings(merge_ai_settings(payload, current))
    return _run_ai_connectivity_test(request, ai_settings)

class AdminUserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"

class AdminUserUpdateRequest(BaseModel):
    role: Optional[str] = None
    enabled: Optional[bool] = None

class AdminUserResetPasswordRequest(BaseModel):
    password: str

class AdminUserPageResponse(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    page: int
    pageSize: int


ADMIN_ROLES = {"admin", "operator", "viewer"}


def _normalize_admin_role(role: str) -> str:
    normalized = str(role or "viewer").strip().lower() or "viewer"
    if normalized not in ADMIN_ROLES:
        raise HTTPException(status_code=400, detail="Invalid admin role")
    return normalized


@router.get("/admin-users/page", response_model=AdminUserPageResponse)
def read_admin_users_page(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_admin_manage),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    q: Optional[str] = Query(None, max_length=60),
):
    tenant_id = tenant_id_from_payload(admin)
    stmt = select(AdminUser).where(_tenant_filter(AdminUser.tenant_id, tenant_id))
    if q:
        qq = q.strip()
        stmt = stmt.where(AdminUser.username.contains(qq))
    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    rows = session.exec(
        stmt.order_by(AdminUser.id.desc()).offset((page - 1) * pageSize).limit(pageSize)
    ).all()
    items = [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
            "username": r.username,
            "role": r.role,
            "enabled": r.enabled,
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "pageSize": pageSize}

@router.post("/admin-users")
def create_admin_user(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_admin_manage),
    req: AdminUserCreateRequest,
):
    tenant_id = tenant_id_from_payload(admin)
    username = str(req.username or "").strip()
    if not username or username.startswith("app:") or len(username) > 64:
        raise HTTPException(status_code=400, detail="Invalid username")
    role = _normalize_admin_role(req.role)
    password = require_password_strength(str(req.password or "").strip(), min_length=12, label="Admin password")
    existing = session.exec(
        select(AdminUser).where((AdminUser.tenant_id == tenant_id) & (AdminUser.username == username))
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Admin user already exists")
    user = AdminUser(
        tenant_id=tenant_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        enabled=True,
    )
    session.add(user)
    session.flush()
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=admin.get("sub"),
            action="admin_user.create",
            target_user_id=None,
            detail={"id": user.id, "username": username, "role": role},
        )
    )
    session.commit()
    session.refresh(user)
    return {
        "id": user.id,
        "created_at": user.created_at.isoformat(sep=" ", timespec="seconds"),
        "username": user.username,
        "role": user.role,
        "enabled": user.enabled,
    }

@router.patch("/admin-users/{admin_user_id}")
def update_admin_user(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_admin_manage),
    admin_user_id: int,
    req: AdminUserUpdateRequest,
):
    tenant_id = tenant_id_from_payload(admin)
    user = session.get(AdminUser, admin_user_id)
    if user and not _tenant_matches(getattr(user, "tenant_id", None), tenant_id):
        user = None
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    changed: List[str] = []
    if req.role is not None:
        role = _normalize_admin_role(req.role)
        if user.role == "admin" and role != "admin":
            enabled_admins = session.exec(
                select(func.count())
                .select_from(AdminUser)
                .where(
                    (AdminUser.tenant_id == tenant_id)
                    & (AdminUser.role == "admin")
                    & (AdminUser.enabled.is_(True))
                )
            ).one()
            if enabled_admins <= 1 and user.enabled is True:
                raise HTTPException(status_code=400, detail="至少保留一个启用的管理员")
        if user.role != role:
            user.role = role
            user.token_version = int(user.token_version or 0) + 1
        changed.append("role")
    if req.enabled is not None:
        enabled = bool(req.enabled)
        if not enabled and user.role == "admin":
            enabled_admins = session.exec(
                select(func.count())
                .select_from(AdminUser)
                .where(
                    (AdminUser.tenant_id == tenant_id)
                    & (AdminUser.role == "admin")
                    & (AdminUser.enabled.is_(True))
                )
            ).one()
            if enabled_admins <= 1:
                raise HTTPException(status_code=400, detail="至少保留一个启用的管理员")
        if user.enabled != enabled:
            user.enabled = enabled
            user.token_version = int(user.token_version or 0) + 1
        changed.append("enabled")
    session.add(user)
    session.add(AuditLog(tenant_id=tenant_id, actor=admin.get("sub"), action="admin_user.update", target_user_id=None, detail={"id": admin_user_id, "fields": changed}))
    session.commit()
    return {"ok": True}

@router.post("/admin-users/{admin_user_id}/reset-password")
def reset_admin_user_password(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_admin_manage),
    admin_user_id: int,
    req: AdminUserResetPasswordRequest,
):
    tenant_id = tenant_id_from_payload(admin)
    user = session.get(AdminUser, admin_user_id)
    if user and not _tenant_matches(getattr(user, "tenant_id", None), tenant_id):
        user = None
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    password = (req.password or "").strip()
    if not password or len(password) < 6 or len(password) > 100:
        raise HTTPException(status_code=400, detail="密码长度需为 6-100")
    password = require_password_strength(password, min_length=12, label="Admin password")
    user.password_hash = hash_password(password)
    user.token_version = int(user.token_version or 0) + 1
    session.add(user)
    session.add(AuditLog(tenant_id=tenant_id, actor=admin.get("sub"), action="admin_user.reset_password", target_user_id=None, detail={"id": admin_user_id, "username": user.username}))
    session.commit()
    return {"ok": True}

@router.post("/users", response_model=UserRead)
def create_user(*, session: Session = Depends(get_session), user: UserCreate, operator: dict = Depends(require_users_write)):
    db_user = User.model_validate(user)
    db_user.tenant_id = tenant_id_from_payload(operator)
    db_user.password = encrypt_secret(db_user.password)
    db_user.ai = {}
    db_user.pushNotifications = normalize_push_notifications(db_user.pushNotifications)
    _ensure_clockin_schedule_defaults(db_user)
    session.add(db_user)
    session.flush()
    session.add(AuditLog(tenant_id=db_user.tenant_id, actor=operator.get("sub"), action="user.create", target_user_id=db_user.id, detail={"phone": db_user.phone}))
    session.commit()
    session.refresh(db_user)
    if db_user.enable_clockin or _any_report_enabled(db_user):
        add_user_job(db_user)
    return _sanitize_user_for_read(db_user)

@router.get("/users", response_model=List[UserRead])
def read_users(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(require_users_read),
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    tenant_id = tenant_id_from_payload(admin)
    users = session.exec(
        select(User)
        .where(_tenant_filter(User.tenant_id, tenant_id) & (User.deleted_at.is_(None)))
        .order_by(User.id.asc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_sanitize_user_for_read(u) for u in users]

class UserPageResponse(BaseModel):
    items: List[UserListRead]
    total: int
    page: int
    pageSize: int
    q: Optional[str] = None

@router.get("/users/page", response_model=UserPageResponse)
def read_users_page(
    *,
    session: Session = Depends(get_session),
    viewer: dict = Depends(require_users_read),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    q: Optional[str] = Query(None, min_length=1, max_length=50),
):
    tenant_id = tenant_id_from_payload(viewer)
    cond = None
    if q:
        qq = q.strip()
        cond = ((User.phone.contains(qq)) | (func.coalesce(User.remark, "").contains(qq))) & (User.deleted_at.is_(None)) & _tenant_filter(User.tenant_id, tenant_id)
    else:
        cond = (User.deleted_at.is_(None)) & _tenant_filter(User.tenant_id, tenant_id)

    count_stmt = select(func.count()).select_from(User)
    count_stmt = count_stmt.where(cond)
    total = session.exec(count_stmt).one()

    stmt = select(
        User.id,
        User.phone,
        User.remark,
        User.enable_clockin,
        User.last_run_time,
        User.last_status,
        User.logs,
    )
    stmt = stmt.where(cond)
    rows = session.exec(
        stmt.order_by(User.id.desc()).offset((page - 1) * pageSize).limit(pageSize)
    ).all()
    items = [
        UserListRead(
            id=r.id,
            phone=r.phone,
            remark=r.remark,
            enable_clockin=r.enable_clockin,
            last_run_time=r.last_run_time,
            last_status=r.last_status,
            logs=r.logs or [],
        )
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "pageSize": pageSize, "q": q}

@router.get("/users/{user_id}", response_model=UserRead)
def read_user(*, session: Session = Depends(get_session), user_id: int, viewer: dict = Depends(require_users_read)):
    user = _get_active_user_for_payload(session, user_id, viewer)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _sanitize_user_for_read(user)

@router.get("/users/{user_id}/execution")
def read_user_execution(*, session: Session = Depends(get_session), user_id: int, viewer: dict = Depends(require_users_read)):
    user = _get_active_user_for_payload(session, user_id, viewer)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"results": user.last_execution_result or []}

@router.get("/users/{user_id}/job-info")
def read_user_job_info(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    operator: dict = Depends(require_users_read),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"job_info:{client_ip}:{user_id}", limit=3, per_seconds=60)
    user = _get_active_user_for_payload(session, user_id, operator)
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法自动获取岗位信息")

    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    _ensure_remote_runtime(api_client, config)
    job_info = api_client.get_job_info() or {}
    if sync_runtime_fields_to_user(user, config_data):
        session.add(user)
        session.commit()
    company = job_info.get("practiceCompanyEntity") or {}
    candidates = []
    for k in [
        "jobAddress",
        "address",
        "detailAddress",
        "jobDetailAddress",
        "practiceAddress",
        "practiceDetailAddress",
        "companyAddress",
        "workAddress",
    ]:
        v = job_info.get(k)
        if isinstance(v, str):
            s = v.strip()
            if s and s not in candidates:
                candidates.append(s)
    job_address = job_info.get("jobAddress")
    if isinstance(job_address, str):
        job_address = job_address.strip()
    if not job_address:
        job_address = candidates[0] if candidates else None
    return {
        "ok": True,
        "jobId": job_info.get("jobId"),
        "jobAddress": job_address,
        "addressCandidates": candidates,
        "companyName": company.get("companyName"),
        "quartersIntroduce": job_info.get("quartersIntroduce"),
    }


@router.get("/users/{user_id}/account-address")
def read_user_account_address(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    operator: dict = Depends(require_users_read),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"account_addr:{client_ip}:{user_id}", limit=3, per_seconds=60)
    user = _get_active_user_for_payload(session, user_id, operator)
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法自动获取账号地址")

    try:
        config_data = user_to_config(user)
        config = ConfigManager(config=config_data)
        api_client = ApiClient(config)
        _ensure_remote_runtime(api_client, config)
        checkin = api_client.get_checkin_info() or {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "获取账号地址失败")

    candidates: List[str] = []
    for k in [
        "address",
        "detailAddress",
        "lastDetailAddress",
        "lastAddress",
        "practiceAddress",
        "practiceDetailAddress",
        "companyAddress",
        "workAddress",
    ]:
        v = checkin.get(k)
        if isinstance(v, str):
            s = v.strip()
            if s and s not in candidates:
                candidates.append(s)

    best = candidates[0] if candidates else None
    if candidates:
        best = sorted(candidates, key=lambda x: len(x), reverse=True)[0]
    if not best:
        raise HTTPException(status_code=404, detail="未获取到账号地址（可能该账号暂无打卡记录）")

    try:
        clock_in = user.clockIn if isinstance(user.clockIn, dict) else {}
        loc = clock_in.get("location") if isinstance(clock_in.get("location"), dict) else {}
        loc2 = dict(loc)
        loc2["address"] = best
        parts = [p.strip() for p in re.split(r"\s*[·,/，,]\s*", str(best)) if p and p.strip()]
        if len(parts) >= 1 and not loc2.get("province"):
            loc2["province"] = parts[0]
        if len(parts) >= 2 and not loc2.get("city"):
            loc2["city"] = parts[1]
        if len(parts) >= 3 and not loc2.get("area"):
            loc2["area"] = parts[2]
        clock_in2 = dict(clock_in)
        clock_in2["location"] = loc2
        user.clockIn = clock_in2
        sync_runtime_fields_to_user(user, config_data)
        session.add(user)
        session.commit()
    except Exception:
        session.rollback()

    return {
        "ok": True,
        "address": best,
        "addressCandidates": candidates,
        "maskedAddress": _mask_address(best),
        "maskedCandidates": [_mask_address(x) for x in candidates],
        "checkinTime": checkin.get("attendenceTime"),
        "type": checkin.get("type"),
    }


@router.get("/users/{user_id}/clock-in/missing-days")
def read_user_clockin_missing_days(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    operator: dict = Depends(require_users_read),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"clockin_missing:{client_ip}:{user_id}", limit=10, per_seconds=60)
    user = _get_active_user_for_payload(session, user_id, operator)
    try:
        return _get_missing_clockin_days_for_user(user)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "获取缺卡日期失败")


@router.post("/users/{user_id}/clock-in/makeup")
def makeup_user_clockin(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    req: ClockInMakeupRequest,
    operator: dict = Depends(require_tasks_run),
):
    client_ip = get_client_ip(request)
    idempotency_key = _normalize_idempotency_key(
        request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    )
    tenant_id = tenant_id_from_payload(operator)
    request_hash = ""
    user = _get_active_user_for_payload(session, user_id, operator)
    try:
        target_dates = _clockin_makeup_dates_from_request(req)
        target_type = _clockin_makeup_type_from_request(req)
        tenant_id = tenant_id_from_payload(operator)
        request_hash = _operation_request_hash(
            {
                "actor": str(operator.get("sub") or ""),
                "operation": "user.clockin.makeup",
                "target_dates": target_dates,
                "target_type": target_type,
                "tenant_id": tenant_id,
                "user_id": int(user_id),
            }
        )
        if idempotency_key:
            existing_response = _manual_operation_existing_response(
                session,
                tenant_id=tenant_id,
                operation="user.clockin.makeup",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if existing_response is not None:
                return existing_response
        _rate_limit(f"clockin_makeup:{client_ip}:{user_id}", limit=3, per_seconds=60)
        if idempotency_key:
            claimed_response = claim_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("user.clockin.makeup", idempotency_key),
                request_hash=request_hash,
                conflict_detail="Idempotency key already used with different request parameters",
                pending_detail="Request is already being processed",
            )
            if claimed_response is not None:
                return claimed_response
        result, config_data = _makeup_clockin_for_user(user, target_dates, target_type)
        sync_runtime_fields_to_user(user, config_data)
        response_payload = {"ok": result.get("status") != "fail", "result": result}
        if idempotency_key:
            finalize_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("user.clockin.makeup", idempotency_key),
                request_hash=request_hash,
                response=response_payload,
            )
        session.add(AuditLog(tenant_id=user.tenant_id, actor=operator.get("sub"), action="user.clockin.makeup", target_user_id=user_id, detail={"target_dates": target_dates, "target_type": target_type, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return response_payload
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        if idempotency_key:
            try:
                finalize_idempotency_record(
                    db_engine=engine,
                    tenant_id=tenant_id,
                    logical_key=_manual_operation_idempotency_storage_key("user.clockin.makeup", idempotency_key),
                    request_hash=request_hash,
                    status=IDEMPOTENCY_STATUS_FAILED,
                    error=str(e),
                )
            except Exception:
                logger.debug("failed to finalize idempotency state", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e) or "补卡失败")


@router.post("/users/{user_id}/clock-in/makeup-all")
def makeup_all_user_clockin(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    req: Optional[ClockInMakeupRequest] = None,
    operator: dict = Depends(require_tasks_run),
):
    client_ip = get_client_ip(request)
    idempotency_key = _normalize_idempotency_key(
        request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    )
    tenant_id = tenant_id_from_payload(operator)
    request_hash = ""
    user = _get_active_user_for_payload(session, user_id, operator)
    try:
        target_type = _clockin_makeup_type_from_request(req or ClockInMakeupRequest())
        tenant_id = tenant_id_from_payload(operator)
        request_hash = _operation_request_hash(
            {
                "actor": str(operator.get("sub") or ""),
                "operation": "user.clockin.makeup_all",
                "target_type": target_type,
                "tenant_id": tenant_id,
                "user_id": int(user_id),
            }
        )
        if idempotency_key:
            existing_response = _manual_operation_existing_response(
                session,
                tenant_id=tenant_id,
                operation="user.clockin.makeup_all",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if existing_response is not None:
                return existing_response
        _rate_limit(f"clockin_makeup_all:{client_ip}:{user_id}", limit=2, per_seconds=60)
        if idempotency_key:
            claimed_response = claim_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("user.clockin.makeup_all", idempotency_key),
                request_hash=request_hash,
                conflict_detail="Idempotency key already used with different request parameters",
                pending_detail="Request is already being processed",
            )
            if claimed_response is not None:
                return claimed_response
        result, config_data, target_dates = _makeup_all_missing_clockin_for_user(user, target_type)
        sync_runtime_fields_to_user(user, config_data)
        response_payload = {"ok": result.get("status") != "fail", "result": result, "target_dates": target_dates, "target_type": target_type}
        if idempotency_key:
            finalize_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("user.clockin.makeup_all", idempotency_key),
                request_hash=request_hash,
                response=response_payload,
            )
        session.add(AuditLog(tenant_id=user.tenant_id, actor=operator.get("sub"), action="user.clockin.makeup_all", target_user_id=user_id, detail={"target_dates": target_dates, "target_type": target_type, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return response_payload
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        if idempotency_key:
            try:
                finalize_idempotency_record(
                    db_engine=engine,
                    tenant_id=tenant_id,
                    logical_key=_manual_operation_idempotency_storage_key("user.clockin.makeup_all", idempotency_key),
                    request_hash=request_hash,
                    status=IDEMPOTENCY_STATUS_FAILED,
                    error=str(e),
                )
            except Exception:
                logger.debug("failed to finalize idempotency state", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e) or "全部补卡失败")

@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(*, session: Session = Depends(get_session), user_id: int, user_update: UserUpdate, operator: dict = Depends(require_users_write)):
    db_user = _get_active_user_for_payload(session, user_id, operator)

    user_data = user_update.dict(exclude_unset=True)
    if "password" in user_data:
        user_data["password"] = encrypt_secret(str(user_data.get("password") or ""))
    user_data.pop("ai", None)
    if "pushNotifications" in user_data:
        user_data["pushNotifications"] = normalize_push_notifications(
            user_data.get("pushNotifications"),
            db_user.pushNotifications,
        )
    for key, value in user_data.items():
        setattr(db_user, key, value)
    _ensure_clockin_schedule_defaults(db_user)

    session.add(db_user)
    session.add(AuditLog(tenant_id=db_user.tenant_id, actor=operator.get("sub"), action="user.update", target_user_id=user_id, detail={"fields": list(user_data.keys())}))
    session.commit()
    session.refresh(db_user)

    remove_user_job(user_id)
    if db_user.enable_clockin or _any_report_enabled(db_user):
        add_user_job(db_user)

    return _sanitize_user_for_read(db_user)

@router.delete("/users/{user_id}")
def delete_user(*, session: Session = Depends(get_session), user_id: int, admin: dict = Depends(require_users_delete)):
    user = _get_active_user_or_404(session, user_id, tenant_id_from_payload(admin))

    remove_user_job(user_id)
    user.deleted_at = utc_now()
    user.deleted_by = str(admin.get("sub") or "admin")
    user.delete_reason = "admin_delete"
    user.enable_clockin = False
    user.app_enabled = False
    for app_user in session.exec(select(AppUser).where(AppUser.bound_user_id == user_id)).all():
        app_user.enabled = False
        app_user.token_version = int(app_user.token_version or 0) + 1
        session.add(app_user)
    session.add(user)
    session.add(AuditLog(tenant_id=user.tenant_id, actor=admin.get("sub"), action="user.soft_delete", target_user_id=user_id, detail={}))
    session.commit()
    return {"ok": True}

@router.post("/users/{user_id}/run")
def run_user_task(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    req: Optional[AppRunRequest] = None,
    operator: dict = Depends(require_tasks_run),
):
    client_ip = get_client_ip(request)
    idempotency_key = _normalize_idempotency_key(
        request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    )
    tenant_id = tenant_id_from_payload(operator)
    request_payload = {
        "actor": str(operator.get("sub") or ""),
        "force_report": bool(req.force_report) if req else False,
        "operation": "user.run",
        "target_period": req.target_period if req else None,
        "task_type": req.task_type if req else None,
        "tenant_id": tenant_id,
        "user_id": int(user_id),
    }
    request_hash = _operation_request_hash(request_payload)
    if idempotency_key:
        existing_response = _manual_operation_existing_response(
            session,
            tenant_id=tenant_id,
            operation="user.run",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if existing_response is not None:
            return existing_response
    if _should_rate_limit_run_request(req):
        _rate_limit(f"run:{client_ip}:{user_id}", limit=2, per_seconds=60)
    user = _get_active_user_for_payload(session, user_id, operator)
    if idempotency_key:
        claimed_response = claim_idempotency_record(
            db_engine=engine,
            tenant_id=tenant_id,
            logical_key=_manual_operation_idempotency_storage_key("user.run", idempotency_key),
            request_hash=request_hash,
            conflict_detail="Idempotency key already used with different request parameters",
            pending_detail="Request is already being processed",
        )
        if claimed_response is not None:
            return claimed_response

    try:
        config_data = user_to_config(user)
        specific_task_type = req.task_type if req else None
        results = run_task_by_config(
            config_data,
            specific_task_type=specific_task_type,
            force_report=bool(req.force_report) if req else False,
            target_period=req.target_period if req else None,
            preauthorization_hooks=build_preauthorization_hooks(user, db_engine=engine),
        )
        status = apply_execution_results_to_user(user, results, config_data)
        response_payload = {"results": results}
        if idempotency_key:
            finalize_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("user.run", idempotency_key),
                request_hash=request_hash,
                response={"results": sanitize_execution_results(results)},
            )
        session.add(AuditLog(tenant_id=user.tenant_id, actor=operator.get("sub"), action="user.run", target_user_id=user_id, detail={"status": status}))
        session.add(user)
        session.commit()
        return response_payload
    except Exception as e:
        session.rollback()
        if idempotency_key:
            try:
                finalize_idempotency_record(
                    db_engine=engine,
                    tenant_id=tenant_id,
                    logical_key=_manual_operation_idempotency_storage_key("user.run", idempotency_key),
                    request_hash=request_hash,
                    status=IDEMPOTENCY_STATUS_FAILED,
                    error=str(e),
                )
            except Exception:
                logger.debug("failed to finalize idempotency state", exc_info=True)
        raise


@router.post("/users/{user_id}/clock-in/alipay/continue")
def continue_user_alipay_clockin(
    *,
    request: Request,
    req: AlipayClockInContinueRequest,
    session: Session = Depends(get_session),
    user_id: int,
    operator: dict = Depends(require_tasks_run),
):
    out_register_no, target_type = _alipay_continue_values(req)
    client_ip = get_client_ip(request)
    _rate_limit(f"run:{client_ip}:{user_id}", limit=2, per_seconds=60)
    user = _get_active_user_for_payload(session, user_id, operator)

    result, _ = _continue_alipay_clockin_for_user(
        user,
        out_register_no,
        target_type,
    )
    session.add(
        AuditLog(
            tenant_id=user.tenant_id,
            actor=operator.get("sub"),
            action="user.clockin.alipay.continue",
            target_user_id=user_id,
            detail={"status": result.get("status"), "target_type": target_type},
        )
    )
    session.add(user)
    session.commit()
    return {"result": result}


@router.get(
    "/users/{user_id}/clock-in/preauthorizations",
    response_model=ClockInPreauthorizationListResponse,
)
def read_user_clockin_preauthorizations(
    *,
    request: Request,
    user_id: int,
    scope: str = Query("future"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    target_type: Optional[str] = Query(None),
    refresh_plan: bool = Query(False),
    session: Session = Depends(get_session),
    operator: dict = Depends(require_tasks_run),
):
    user = _get_active_user_for_payload(session, user_id, operator)
    client_ip = get_client_ip(request)
    _rate_limit(f"preauthorization_list:{client_ip}:{user_id}", limit=30, per_seconds=60)
    try:
        return _list_clockin_preauthorizations_for_user(
            session,
            user,
            scope=scope,
            page=page,
            page_size=page_size,
            status=status,
            target_type=target_type,
            refresh_plan=refresh_plan,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/users/{user_id}/clock-in/preauthorizations/start",
    response_model=ClockInPreauthorizationStartResponse,
)
def start_user_clockin_preauthorization(
    *,
    request: Request,
    req: ClockInPreauthorizationStartRequest,
    user_id: int,
    session: Session = Depends(get_session),
    operator: dict = Depends(require_tasks_run),
):
    user = _get_active_user_for_payload(session, user_id, operator)
    client_ip = get_client_ip(request)
    _rate_limit(
        f"preauthorization_start:{client_ip}:{user_id}",
        limit=CLOCKIN_PREAUTHORIZATION_ACTION_LIMIT,
        per_seconds=60,
    )
    try:
        result = _start_clockin_preauthorization_for_user(user, req, session=session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    target_date, target_type = _preauthorization_target_values(req)
    session.add(user)
    session.add(
        AuditLog(
            tenant_id=user.tenant_id,
            actor=str(operator.get("sub")),
            action="user.clockin.preauthorization.start",
            target_user_id=user.id,
            detail={
                "target_date": target_date.isoformat(),
                "target_type": target_type,
                "status": "started",
            },
        )
    )
    session.commit()
    return result


@router.post(
    "/users/{user_id}/clock-in/preauthorizations/complete",
    response_model=ClockInPreauthorizationCompleteResponse,
)
def complete_user_clockin_preauthorization(
    *,
    request: Request,
    req: ClockInPreauthorizationCompleteRequest,
    user_id: int,
    session: Session = Depends(get_session),
    operator: dict = Depends(require_tasks_run),
):
    user = _get_active_user_for_payload(session, user_id, operator)
    client_ip = get_client_ip(request)
    _rate_limit(
        f"preauthorization_complete:{client_ip}:{user_id}",
        limit=CLOCKIN_PREAUTHORIZATION_ACTION_LIMIT,
        per_seconds=60,
    )
    try:
        result = _complete_clockin_preauthorization_for_user(
            session,
            user,
            req.registration_ticket,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session.add(
        AuditLog(
            tenant_id=user.tenant_id,
            actor=str(operator.get("sub")),
            action="user.clockin.preauthorization.complete",
            target_user_id=user.id,
            detail={
                "target_date": result["target_date"],
                "target_type": result["target_type"],
                "status": result["status"],
            },
        )
    )
    session.commit()
    return {"item": result}


class BatchRunRequest(BaseModel):
    ids: List[int]
    concurrency: int = 5

@router.post("/users/run/batch")
def run_users_batch(*, request: Request, req: BatchRunRequest, operator: dict = Depends(require_batch_manage)):
    client_ip = get_client_ip(request)
    idempotency_key = _normalize_idempotency_key(
        request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    )
    try:
        ids = [int(x) for x in (req.ids or []) if int(x) > 0]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user id list")
    if not ids:
        raise HTTPException(status_code=400, detail="请选择要运行的账号")
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="Duplicate user ids are not allowed")
    max_users = int_env("BATCH_JOB_MAX_USERS", 200, min_value=1, max_value=5000)
    if len(ids) > max_users:
        raise HTTPException(status_code=400, detail=f"Batch job cannot include more than {max_users} users")
    max_concurrency = int(os.getenv("BATCH_JOB_MAX_CONCURRENCY") or "10")
    max_concurrency = max(1, min(max_concurrency, 50))
    concurrency = max(1, min(int(req.concurrency or 5), max_concurrency))
    tenant_id = tenant_id_from_payload(operator)
    actor = str(operator.get("sub") or "")
    request_hash = _batch_run_request_hash(ids=ids, concurrency=concurrency, tenant_id=tenant_id, actor=actor)
    if idempotency_key:
        with Session(engine) as session:
            existing_response = _batch_run_existing_response(
                session,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if existing_response is not None:
                return existing_response
    _rate_limit(f"run_batch:{client_ip}", limit=1, per_seconds=10)
    if idempotency_key:
        claimed_response = claim_idempotency_record(
            db_engine=engine,
            tenant_id=tenant_id,
            logical_key=_batch_run_idempotency_storage_key(idempotency_key),
            request_hash=request_hash,
            conflict_detail="Idempotency key already used with different batch parameters",
            pending_detail="Batch request is already being processed",
        )
        if claimed_response is not None:
            return claimed_response
    with Session(engine) as session:
        try:
            active_limit = int_env("BATCH_TENANT_MAX_ACTIVE_JOBS", 5, min_value=1, max_value=100)
            active_jobs = session.exec(
                select(func.count())
                .select_from(BatchJob)
                .where(_tenant_filter(BatchJob.tenant_id, tenant_id) & (BatchJob.status.in_(["queued", "running", "paused"])))
            ).one()
            if int(active_jobs or 0) >= active_limit:
                raise HTTPException(status_code=429, detail="Too many active batch jobs for this tenant")
            valid_ids = session.exec(
                select(User.id).where(_tenant_filter(User.tenant_id, tenant_id) & (User.deleted_at.is_(None)) & (User.id.in_(ids)))
            ).all()
            if len(valid_ids) != len(set(ids)):
                raise HTTPException(status_code=404, detail="User not found")
            job = BatchJob(tenant_id=tenant_id, created_by=operator.get("sub"), total=len(ids), concurrency=concurrency, user_ids=ids, status="queued")
            session.add(job)
            session.flush()
            items = [BatchJobItem(tenant_id=tenant_id, job_id=job.id, user_id=uid, status="queued") for uid in ids]
            session.add_all(items)
            response_payload = {"ok": True, "queued": len(ids), "concurrency": concurrency, "job_id": job.id}
            if idempotency_key:
                finalize_idempotency_record(
                    db_engine=engine,
                    tenant_id=tenant_id,
                    logical_key=_batch_run_idempotency_storage_key(idempotency_key),
                    request_hash=request_hash,
                    response=response_payload,
                )
            session.add(AuditLog(tenant_id=tenant_id, actor=operator.get("sub"), action="batch.enqueue", target_user_id=None, detail={"job_id": job.id, "total": len(ids), "concurrency": concurrency}))
            session.commit()
            return response_payload
        except Exception as exc:
            session.rollback()
            if idempotency_key:
                try:
                    finalize_idempotency_record(
                        db_engine=engine,
                        tenant_id=tenant_id,
                        logical_key=_batch_run_idempotency_storage_key(idempotency_key),
                        request_hash=request_hash,
                        status=IDEMPOTENCY_STATUS_FAILED,
                        error=str(exc),
                    )
                except Exception:
                    logger.debug("failed to finalize idempotency state", exc_info=True)
            raise

@router.get("/batch-jobs/{job_id}")
def read_batch_job(*, session: Session = Depends(get_session), job_id: int = 0, viewer: dict = Depends(require_batch_read)):
    tenant_id = tenant_id_from_payload(viewer)
    job = session.exec(select(BatchJob).where((BatchJob.id == job_id) & _tenant_filter(BatchJob.tenant_id, tenant_id))).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    status_counts = get_batch_job_item_status_counts(session, job_id, ["running", "queued", "fail"])
    last_errors = session.exec(
        select(BatchJobItem.user_id, BatchJobItem.error, BatchJobItem.finished_at)
        .where((BatchJobItem.job_id == job_id) & (BatchJobItem.status == "fail"))
        .order_by(BatchJobItem.id.desc())
        .limit(20)
    ).all()
    return {
        "id": job.id,
        "created_at": job.created_at.isoformat(sep=" ", timespec="seconds"),
        "created_by": job.created_by,
        "status": job.status,
        "total": job.total,
        "completed": job.completed,
        "success": job.success,
        "fail": job.fail,
        "concurrency": job.concurrency,
        "running": int(status_counts.get("running", 0)),
        "queued": int(status_counts.get("queued", 0)),
        "failed": int(status_counts.get("fail", 0)),
        "last_errors": [
            {"user_id": row.user_id, "message": row.error or "Fail", "ts": (row.finished_at.isoformat() if row.finished_at else None)}
            for row in last_errors
        ],
    }


@router.post("/batch-jobs/{job_id}/retry-failed")
def retry_failed_batch_job_items(*, session: Session = Depends(get_session), job_id: int, operator: dict = Depends(require_batch_manage)):
    tenant_id = tenant_id_from_payload(operator)
    job = session.exec(select(BatchJob).where((BatchJob.id == job_id) & _tenant_filter(BatchJob.tenant_id, tenant_id))).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.cancel_requested or str(job.status or "").lower() == "canceled":
        raise HTTPException(status_code=409, detail="Canceled batch job cannot be retried")

    failed_items = session.exec(
        select(BatchJobItem).where((BatchJobItem.job_id == job_id) & (BatchJobItem.status == "fail"))
    ).all()
    if not failed_items:
        return {"ok": True, "job_id": job.id, "requeued": 0, "status": job.status}

    requeued = 0
    for item in failed_items:
        item.status = "queued"
        item.attempts = 0
        item.started_at = None
        item.finished_at = None
        item.error = None
        item.next_run_at = None
        item.locked_by = None
        item.lock_token = None
        item.lease_until = None
        item.heartbeat_at = None
        session.add(item)
        requeued += 1

    running_count = session.exec(
        select(func.count()).select_from(BatchJobItem).where((BatchJobItem.job_id == job_id) & (BatchJobItem.status == "running"))
    ).one()
    job.completed = max(0, int(job.completed or 0) - requeued)
    job.fail = max(0, int(job.fail or 0) - requeued)
    job.finished_at = None
    if job.paused:
        job.status = "paused"
    elif int(running_count or 0) > 0:
        job.status = "running"
    else:
        job.status = "queued"
    session.add(job)
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=operator.get("sub"),
            action="batch.retry_failed",
            target_user_id=None,
            detail={"job_id": job_id, "requeued": requeued},
        )
    )
    session.commit()
    return {"ok": True, "job_id": job.id, "requeued": requeued, "status": job.status}

@router.post("/batch-jobs/{job_id}/pause")
def pause_batch_job(*, session: Session = Depends(get_session), job_id: int, operator: dict = Depends(require_batch_manage)):
    tenant_id = tenant_id_from_payload(operator)
    job = session.exec(select(BatchJob).where((BatchJob.id == job_id) & _tenant_filter(BatchJob.tenant_id, tenant_id))).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.paused = True
    session.add(job)
    session.add(AuditLog(tenant_id=tenant_id, actor=operator.get("sub"), action="batch.pause", target_user_id=None, detail={"job_id": job_id}))
    session.commit()
    return {"ok": True}

@router.post("/batch-jobs/{job_id}/resume")
def resume_batch_job(*, session: Session = Depends(get_session), job_id: int, operator: dict = Depends(require_batch_manage)):
    tenant_id = tenant_id_from_payload(operator)
    job = session.exec(select(BatchJob).where((BatchJob.id == job_id) & _tenant_filter(BatchJob.tenant_id, tenant_id))).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.paused = False
    if job.status in ["paused", "queued"]:
        job.status = "queued"
    session.add(job)
    session.add(AuditLog(tenant_id=tenant_id, actor=operator.get("sub"), action="batch.resume", target_user_id=None, detail={"job_id": job_id}))
    session.commit()
    return {"ok": True}

@router.post("/batch-jobs/{job_id}/cancel")
def cancel_batch_job(*, session: Session = Depends(get_session), job_id: int, operator: dict = Depends(require_batch_manage)):
    tenant_id = tenant_id_from_payload(operator)
    job = session.exec(select(BatchJob).where((BatchJob.id == job_id) & _tenant_filter(BatchJob.tenant_id, tenant_id))).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.cancel_requested = True
    session.add(job)
    session.add(AuditLog(tenant_id=tenant_id, actor=operator.get("sub"), action="batch.cancel", target_user_id=None, detail={"job_id": job_id}))
    session.commit()
    return {"ok": True}

def _run_ai_connectivity_test(request: Request, ai_settings: Dict[str, Any]) -> Dict[str, Any]:
    client_ip = get_client_ip(request)
    _rate_limit(f"ai_test:{client_ip}", limit=5, per_seconds=60)
    api_url = str(ai_settings.get("apiUrl") or "").strip()
    api_key = str(ai_settings.get("apikey") or "").strip()
    model = str(ai_settings.get("model") or "").strip()
    if not api_url or not api_key or not model:
        raise HTTPException(status_code=400, detail="请填写 API URL、API Key 和 Model")
    base = api_url.rstrip("/")
    endpoint = urljoin(base + "/", "chat/completions") if base.endswith("/v1") else urljoin(base + "/", "v1/chat/completions")
    if not _is_safe_outbound_url(endpoint):
        raise HTTPException(status_code=400, detail="AI API URL 不安全（仅允许 https 且禁止内网/本机地址）")
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    t0 = time.time()
    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=20)
        latency_ms = int((time.time() - t0) * 1000)
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except Exception:
                err = {"error": resp.text}
            raise HTTPException(status_code=502, detail=f"AI 接口返回错误: {resp.status_code} {err}")
        data = resp.json()
        content = (
            (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if isinstance(data, dict)
            else None
        )
        return {"ok": True, "latency_ms": latency_ms, "reply": content}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=safe_external_error_detail("AI 接口请求失败", e))


@router.post("/ai/test")
def ai_test(request: Request, req: AiTestRequest, operator: dict = Depends(require_settings_manage)):
    return _run_ai_connectivity_test(
        request,
        {
            "apiUrl": req.apiUrl,
            "apikey": req.apikey,
            "model": req.model,
        },
    )


@router.post("/users/{user_id}/reports/{report_key}/generate")
def generate_report(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    report_key: str,
    target_period: Optional[str] = None,
    operator: dict = Depends(require_tasks_run),
):
    user = _get_active_user_for_payload(session, user_id, operator)
    try:
        generated = _generate_report_content_for_user(user, report_key, target_period)
        sync_runtime_fields_to_user(user, generated["config_data"])
        session.add(
            AuditLog(
                tenant_id=user.tenant_id,
                actor=operator.get("sub"),
                action="user.report.generate",
                target_user_id=user_id,
                detail=_ai_audit_detail(
                    generated["config"],
                    generated["title"],
                    {"report_key": report_key, "target_period": target_period},
                ),
            )
        )
        session.add(user)
        session.commit()
        return {"ok": True, "title": generated["title"], "content": generated["content"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "生成报告失败")


@router.get("/users/{user_id}/reports/{report_key}/missing-periods")
def report_missing_periods(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    report_key: str,
    operator: dict = Depends(require_users_read),
):
    user = _get_active_user_for_payload(session, user_id, operator)
    try:
        return _get_missing_report_periods_for_user(user, report_key)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "获取未提交周期失败")


@router.post("/users/{user_id}/reports/{report_key}/submit")
def submit_report_manual(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    report_key: str,
    req: ReportSubmitRequest,
    operator: dict = Depends(require_tasks_run),
):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="报告内容不能为空")
    user = _get_active_user_for_payload(session, user_id, operator)
    try:
        generated = _generate_report_content_for_user(user, report_key, req.target_period, generate_content=False)
        report_info = _build_report_info(
            api_client=generated["api_client"],
            config=generated["config"],
            meta=generated["meta"],
            content=content,
            target_period=req.target_period,
            submitted=generated["submitted"],
            job_info=generated["job_info"],
        )
        generated["api_client"].submit_report(report_info)
        sync_runtime_fields_to_user(user, generated["config_data"])
        session.add(user)
        session.commit()
        return {"ok": True, "title": report_info["title"], "submitted_at": report_info["reportTime"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "提交报告失败")


@router.post("/users/{user_id}/reports/{report_key}/makeup-all")
def makeup_all_reports_manual(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    report_key: str,
    operator: dict = Depends(require_tasks_run),
):
    idempotency_key = _normalize_idempotency_key(
        request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    )
    tenant_id = tenant_id_from_payload(operator)
    request_hash = ""
    user = _get_active_user_for_payload(session, user_id, operator)
    try:
        tenant_id = tenant_id_from_payload(operator)
        request_hash = _operation_request_hash(
            {
                "actor": str(operator.get("sub") or ""),
                "operation": "user.report.makeup_all",
                "report_key": report_key,
                "tenant_id": tenant_id,
                "user_id": int(user_id),
            }
        )
        if idempotency_key:
            claimed_response = claim_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("user.report.makeup_all", idempotency_key),
                request_hash=request_hash,
                conflict_detail="Idempotency key already used with different request parameters",
                pending_detail="Request is already being processed",
            )
            if claimed_response is not None:
                return claimed_response
        result, config_data, target_periods = _makeup_all_reports_for_user(user, report_key)
        sync_runtime_fields_to_user(user, config_data)
        response_payload = {"ok": result.get("status") != "fail", "result": result, "target_periods": target_periods, "report_key": report_key}
        if idempotency_key:
            finalize_idempotency_record(
                db_engine=engine,
                tenant_id=tenant_id,
                logical_key=_manual_operation_idempotency_storage_key("user.report.makeup_all", idempotency_key),
                request_hash=request_hash,
                response=response_payload,
            )
        session.add(AuditLog(tenant_id=user.tenant_id, actor=operator.get("sub"), action="user.report.makeup_all", target_user_id=user_id, detail={"report_key": report_key, "target_periods": target_periods, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return response_payload
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        if idempotency_key:
            try:
                finalize_idempotency_record(
                    db_engine=engine,
                    tenant_id=tenant_id,
                    logical_key=_manual_operation_idempotency_storage_key("user.report.makeup_all", idempotency_key),
                    request_hash=request_hash,
                    status=IDEMPOTENCY_STATUS_FAILED,
                    error=str(e),
                )
            except Exception:
                logger.debug("failed to finalize idempotency state", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e) or "全部补交报告失败")


@router.post("/users/{user_id}/reports/daily/generate")
def generate_daily_report(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    operator: dict = Depends(require_tasks_run),
):
    user = _get_active_user_for_payload(session, user_id, operator)
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法生成日报")

    try:
        generated = _generate_report_content_for_user(user, "daily", None)
        submitted = generated["submitted"]
        data = submitted.get("data", []) if isinstance(submitted, dict) else []
        already_submitted = False
        current_time = datetime.datetime.now()
        if isinstance(data, list) and data:
            last = data[0]
            ts = last.get("createTime")
            if isinstance(ts, str):
                try:
                    last_time = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    if last_time.date() == current_time.date():
                        already_submitted = True
                except (TypeError, ValueError):
                    logger.debug("ignore invalid submitted daily report createTime: %r", ts)

        sync_runtime_fields_to_user(user, generated["config_data"])
        session.add(user)
        session.add(
            AuditLog(
                tenant_id=user.tenant_id,
                actor=operator.get("sub"),
                action="user.report.daily.generate",
                target_user_id=user_id,
                detail=_ai_audit_detail(generated["config"], generated["title"], {"already_submitted": already_submitted}),
            )
        )
        session.commit()
        return {"ok": True, "title": generated["title"], "content": generated["content"], "already_submitted": already_submitted}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "生成日报失败")


@router.post("/users/{user_id}/reports/daily/submit")
def submit_daily_report_manual(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    req: ReportSubmitRequest,
    operator: dict = Depends(require_users_write),
):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="日报内容不能为空")

    user = _get_active_user_for_payload(session, user_id, operator)
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法提交日报")

    try:
        generated = _generate_report_content_for_user(user, "daily", None, generate_content=False)
        submitted = generated["submitted"]
        data = submitted.get("data", []) if isinstance(submitted, dict) else []
        current_time = datetime.datetime.now()
        if isinstance(data, list) and data:
            last = data[0]
            ts = last.get("createTime")
            if isinstance(ts, str):
                try:
                    last_time = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    if last_time.date() == current_time.date():
                        raise HTTPException(status_code=400, detail="今天已经提交过日报")
                except HTTPException:
                    raise
                except (TypeError, ValueError):
                    logger.debug("ignore invalid submitted daily report createTime: %r", ts)

        title = generated["title"]
        report_info = {
            "title": title,
            "content": content,
            "attachments": "",
            "reportType": "day",
            "jobId": generated["job_info"].get("jobId", None),
            "reportTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "formFieldDtoList": generated["api_client"].get_from_info(7),
        }
        generated["api_client"].submit_report(report_info)
        sync_runtime_fields_to_user(user, generated["config_data"])
        session.add(user)
        session.commit()
        return {"ok": True, "title": title, "submitted_at": report_info["reportTime"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "提交日报失败")


@router.get("/geocode/search")
def geocode_search(q: str = Query(..., min_length=1, max_length=200), operator: dict = Depends(require_geocode_search_access)):
    provider, amap_key, baidu_key = _geocode_search_provider_config()
    baidu_output_coord_type = ""
    if provider == "baidu":
        _, baidu_output_coord_type = _baidu_coord_types()
    q2 = (q or "").strip()
    cache_key = ("search", provider, baidu_output_coord_type, q2) if provider == "baidu" else ("search", provider, q2)
    cached = _geocode_cache_get(cache_key)
    if cached is not None:
        return cached

    if provider == "mapchaxun":
        try:
            resp = requests.post(
                MAPCHAXUN_GEOCODE_URL,
                headers={"content-type": "application/json"},
                data=json.dumps({"address": q2}, separators=(",", ":")),
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            if int(data.get("status", -1)) != 10000:
                raise ValueError(data.get("message") or f"mapchaxun 地理编码错误: {data.get('status')}")
            lon, lat = _mapchaxun_location(data)
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            address = _mapchaxun_address(data)
            label = data.get("adress") or result.get("title") or q2
            out = {
                "results": [
                    {
                        "x": lon,
                        "y": lat,
                        "label": label,
                        "bounds": None,
                        "address": address,
                        "raw": data,
                    }
                ]
            }
            _geocode_cache_set(cache_key, out, ttl_seconds=6 * 60 * 60, maxsize=800)
            return out
        except Exception as e:
            logger.warning("mapchaxun geocode search failed, falling back to osm: %s", e)
            provider = "osm"
            cache_key = ("search", provider, q2)
            cached = _geocode_cache_get(cache_key)
            if cached is not None:
                return cached

    if provider == "baidu":
        try:
            resp = requests.get(
                "https://api.map.baidu.com/geocoding/v3/",
                params={
                    "ak": baidu_key,
                    "address": q2,
                    "output": "json",
                    "ret_coordtype": baidu_output_coord_type,
                },
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            if int(data.get("status", -1)) != 0:
                raise ValueError(data.get("message") or data.get("msg") or f"百度地理编码错误: {data.get('status')}")
            item = data.get("result") or {}
            loc = item.get("location") or {}
            lon = float(loc.get("lng"))
            lat = float(loc.get("lat"))
            label = item.get("formatted_address") or q2
            out = {"results": [{"x": lon, "y": lat, "label": label, "bounds": None, "raw": item}]}
            _geocode_cache_set(cache_key, out, ttl_seconds=6 * 60 * 60, maxsize=800)
            return out
        except Exception as e:
            raise HTTPException(status_code=502, detail=safe_external_error_detail("地理搜索失败", e))

    if provider == "amap":
        try:
            resp = requests.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"key": amap_key, "address": q2, "output": "json"},
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            geocodes = data.get("geocodes") or []
            results: List[Dict[str, Any]] = []
            for item in geocodes[:5]:
                loc = item.get("location")
                if not isinstance(loc, str) or "," not in loc:
                    continue
                lng_s, lat_s = loc.split(",", 1)
                try:
                    lon = float(lng_s)
                    lat = float(lat_s)
                except Exception:
                    continue
                label = item.get("formatted_address") or item.get("address") or q2
                results.append({"x": lon, "y": lat, "label": label, "bounds": None, "raw": item})
            out = {"results": results}
            _geocode_cache_set(cache_key, out, ttl_seconds=6 * 60 * 60, maxsize=800)
            return out
        except Exception as e:
            raise HTTPException(status_code=502, detail=safe_external_error_detail("地理搜索失败", e))

    nominatim_base = NOMINATIM_BASE_URL.rstrip("/")
    params = {"q": q2, "format": "json", "limit": 5, "addressdetails": 1}
    headers = {"User-Agent": "AutoMoGuDingSaaS/1.0", "Accept-Language": "zh-CN,zh;q=0.9"}
    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            resp = requests.get(
                f"{nominatim_base}/search",
                params=params,
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            results: List[Dict[str, Any]] = []
            for item in data or []:
                try:
                    lat = float(item.get("lat"))
                    lon = float(item.get("lon"))
                except Exception:
                    continue
                bbox = item.get("boundingbox")
                bounds: Optional[List[List[float]]] = None
                if isinstance(bbox, list) and len(bbox) == 4:
                    try:
                        south, north, west, east = map(float, bbox)
                        bounds = [[south, west], [north, east]]
                    except Exception:
                        bounds = None
                results.append(
                    {
                        "x": lon,
                        "y": lat,
                        "label": item.get("display_name") or q2,
                        "bounds": bounds,
                        "raw": item,
                    }
                )
            out = {"results": results}
            _geocode_cache_set(cache_key, out, ttl_seconds=60 * 60, maxsize=800)
            return out
        except Exception as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))
    raise HTTPException(status_code=502, detail=safe_external_error_detail("地理搜索失败", last_err))


@router.get("/geocode/reverse")
def geocode_reverse(
    lat: float = Query(...),
    lon: float = Query(...),
    operator: dict = Depends(require_users_write),
):
    provider, amap_key, baidu_key = _geocode_provider_config()
    baidu_input_coord_type = ""
    baidu_output_coord_type = ""
    if provider == "baidu":
        baidu_input_coord_type, baidu_output_coord_type = _baidu_coord_types()
    cache_key = (
        "reverse",
        provider,
        baidu_input_coord_type,
        baidu_output_coord_type,
        round(float(lat), 6),
        round(float(lon), 6),
    ) if provider == "baidu" else ("reverse", provider, round(float(lat), 6), round(float(lon), 6))
    cached = _geocode_cache_get(cache_key)
    if cached is not None:
        return cached

    if provider == "baidu":
        try:
            resp = requests.get(
                "https://api.map.baidu.com/reverse_geocoding/v3/",
                params={
                    "ak": baidu_key,
                    "location": f"{lat},{lon}",
                    "coordtype": baidu_input_coord_type,
                    "ret_coordtype": baidu_output_coord_type,
                    "extensions_poi": 0,
                    "output": "json",
                },
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            if int(data.get("status", -1)) != 0:
                raise ValueError(data.get("message") or data.get("msg") or f"百度逆地理编码错误: {data.get('status')}")
            result = data.get("result") or {}
            formatted = result.get("formatted_address") or ""
            comp = result.get("addressComponent") or {}
            province = comp.get("province") or ""
            city = comp.get("city") or ""
            district = comp.get("district") or ""
            town = comp.get("town") or ""
            street = comp.get("street") or ""
            street_number = comp.get("street_number") or ""
            name = street_number or street or town or district or city or province or ""
            out = {
                "display_name": formatted or name,
                "name": name,
                "address": {
                    "province": province,
                    "city": city,
                    "county": district,
                    "district": district,
                    "town": town,
                    "township": town,
                    "road": street,
                    "house_number": street_number,
                },
                "raw": data,
            }
            out2 = {"result": out}
            _geocode_cache_set(cache_key, out2, ttl_seconds=6 * 60 * 60, maxsize=1200)
            return out2
        except Exception as e:
            raise HTTPException(status_code=502, detail=safe_external_error_detail("逆地理解析失败", e))

    if provider == "amap":
        try:
            resp = requests.get(
                "https://restapi.amap.com/v3/geocode/regeo",
                params={
                    "key": amap_key,
                    "location": f"{lon},{lat}",
                    "radius": 200,
                    "extensions": "base",
                    "output": "json",
                },
                timeout=12,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            regeocode = data.get("regeocode") or {}
            formatted = regeocode.get("formatted_address") or ""
            comp = regeocode.get("addressComponent") or {}
            province = comp.get("province") or ""
            city = comp.get("city") or ""
            if isinstance(city, list):
                city = ""
            district = comp.get("district") or ""
            township = comp.get("township") or ""
            neighborhood = (comp.get("neighborhood") or {}).get("name") if isinstance(comp.get("neighborhood"), dict) else ""
            building = (comp.get("building") or {}).get("name") if isinstance(comp.get("building"), dict) else ""
            name = building or neighborhood or township or district or city or province or ""
            out = {
                "display_name": formatted or name,
                "name": name,
                "address": {
                    "province": province,
                    "city": city,
                    "county": district,
                    "district": district,
                    "town": township,
                    "township": township,
                },
                "raw": data,
            }
            out2 = {"result": out}
            _geocode_cache_set(cache_key, out2, ttl_seconds=6 * 60 * 60, maxsize=1200)
            return out2
        except Exception as e:
            raise HTTPException(status_code=502, detail=safe_external_error_detail("逆地理解析失败", e))

    nominatim_base = NOMINATIM_BASE_URL.rstrip("/")
    params = {"lat": lat, "lon": lon, "format": "json", "zoom": 18, "addressdetails": 1}
    headers = {"User-Agent": "AutoMoGuDingSaaS/1.0", "Accept-Language": "zh-CN,zh;q=0.9"}
    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            resp = requests.get(
                f"{nominatim_base}/reverse",
                params=params,
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            out = {"result": data}
            _geocode_cache_set(cache_key, out, ttl_seconds=60 * 60, maxsize=1200)
            return out
        except Exception as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))
    raise HTTPException(status_code=502, detail=safe_external_error_detail("逆地理解析失败", last_err))
