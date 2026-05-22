from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session, select
from sqlalchemy import func
from server.database import get_session, engine
from server.models import User, UserCreate, UserRead, UserUpdate, UserListRead, AuditLog, BatchJob, BatchJobItem, AdminUser, AppUser, SystemSetting
from server.scheduler import add_user_job, remove_user_job, user_to_config
from server.task_runner import perform_clock_in_makeup, perform_clock_in_makeup_many, run_task_by_config
from server.clockin_backfill import build_missing_clockin_day_options, normalize_clockin_records, parse_clockin_date
from server.util.Config import ConfigManager
from server.coreApi.MainLogicApi import ApiClient
from server.coreApi.AiServiceClient import generate_article
from typing import List, Any, Dict, Optional
import datetime
import requests
from pydantic import BaseModel
from urllib.parse import urljoin, urlparse
import time
import os
import ipaddress
import socket
import re
import threading
from collections import OrderedDict
from server.auth import get_admin, get_operator, get_viewer, get_user, issue_token, get_client_ip, verify_password, hash_password
from server.secret_store import decrypt_secret, encrypt_secret
from server.user_runtime import apply_execution_results_to_user, normalize_push_notifications, normalize_smtp_settings, runtime_login_valid, runtime_plan_required, sync_runtime_fields_to_user
from server.util.MessagePush import send_test_smtp_message

router = APIRouter()

NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"
NOTIFICATION_SETTINGS_KEY = "notifications"
DEFAULT_REPORT_MAKEUP_BATCH_DELAY_SECONDS = 2.0
MAX_REPORT_MAKEUP_BATCH_DELAY_SECONDS = 30.0


def _report_makeup_batch_delay_seconds() -> float:
    raw = os.getenv("REPORT_MAKEUP_BATCH_DELAY_SECONDS") or os.getenv("CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS") or str(DEFAULT_REPORT_MAKEUP_BATCH_DELAY_SECONDS)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_REPORT_MAKEUP_BATCH_DELAY_SECONDS
    if value < 0:
        return 0.0
    return min(value, MAX_REPORT_MAKEUP_BATCH_DELAY_SECONDS)


def _notification_settings_row(session: Session) -> SystemSetting | None:
    return session.get(SystemSetting, NOTIFICATION_SETTINGS_KEY)


def _decode_smtp_settings(raw: Any) -> Dict[str, Any]:
    data = dict(raw) if isinstance(raw, dict) else {}
    if "password" in data:
        try:
            data["password"] = decrypt_secret(str(data.get("password") or ""))
        except Exception:
            data["password"] = ""
    return data


def _get_notification_settings(session: Session) -> Dict[str, Any]:
    row = _notification_settings_row(session)
    value = row.value if row and isinstance(row.value, dict) else {}
    smtp = normalize_smtp_settings(_decode_smtp_settings((value or {}).get("smtp")))
    return {"smtp": smtp}


def _sanitize_notification_settings_for_read(settings: Dict[str, Any]) -> Dict[str, Any]:
    return {"smtp": normalize_smtp_settings(settings.get("smtp"))}


def _save_notification_settings(session: Session, smtp_payload: Dict[str, Any]) -> Dict[str, Any]:
    current = _get_notification_settings(session)
    smtp = normalize_smtp_settings(smtp_payload, current.get("smtp"))
    if not str(smtp.get("username") or "").strip():
        raise HTTPException(status_code=400, detail="请填写 QQ 邮箱")
    if not str(smtp.get("from") or "").strip():
        raise HTTPException(status_code=400, detail="请填写发件人名称")
    if not str(smtp.get("password") or "").strip():
        raise HTTPException(status_code=400, detail="请填写授权码")

    row = _notification_settings_row(session)
    if not row:
        row = SystemSetting(key=NOTIFICATION_SETTINGS_KEY, value={})
    stored_smtp = dict(smtp)
    stored_smtp["password"] = encrypt_secret(str(stored_smtp.get("password") or ""))
    row.value = {"smtp": stored_smtp}
    session.add(row)
    return {"smtp": smtp}

def _is_private_or_special_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
        return bool(
            a.is_private
            or a.is_loopback
            or a.is_link_local
            or a.is_multicast
            or a.is_reserved
            or a.is_unspecified
        )
    except Exception:
        return True

def _is_safe_outbound_url(url: str) -> bool:
    allow_private = (os.getenv("ALLOW_PRIVATE_AI_TEST") or "").strip().lower() in ["1", "true", "yes", "on"]
    if allow_private:
        return True
    u = urlparse(url)
    if u.scheme != "https":
        return False
    host = (u.hostname or "").strip()
    if not host:
        return False
    if host.lower() == "localhost":
        return False
    port = u.port or 443
    if port != 443:
        return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception:
        return False
    for info in infos:
        ip = info[4][0]
        if _is_private_or_special_ip(ip):
            return False
    return True

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
        return app_user

    if not sub.startswith("user:"):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    try:
        legacy_user_id = int(sub.split(":", 1)[1])
    except Exception:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    legacy_user = session.get(User, legacy_user_id)
    if not legacy_user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    if legacy_user.app_enabled is not True:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    if not legacy_user.app_password_hash:
        raise HTTPException(status_code=403, detail="账号未启用用户端登录")
    app_user = session.exec(select(AppUser).where(AppUser.phone == legacy_user.phone)).first()
    if not app_user:
        app_user = AppUser(
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
    if not user:
        raise HTTPException(status_code=403, detail="绑定信息已失效，请重新绑定工学云账号")
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

class ClockInMakeupRequest(BaseModel):
    target_date: Optional[str] = None
    target_dates: Optional[List[str]] = None
    target_type: Optional[str] = None


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


def _makeup_clockin_for_user(user: User, target_dates: List[str], target_type: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    _ensure_remote_runtime(api_client, config)
    if len(target_dates) == 1:
        result = perform_clock_in_makeup(api_client, config, target_dates[0], target_type=target_type)
    else:
        result = perform_clock_in_makeup_many(api_client, config, target_dates, target_type=target_type)
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


def _get_missing_report_periods_for_user(user: User, report_key: str) -> Dict[str, Any]:
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法获取已提交报告")
    meta = _get_report_meta(report_key)
    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    _ensure_remote_runtime(api_client, config)
    report_type = meta["report_type"]
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
            ts = report.get("createTime") or report.get("reportTime")
            if isinstance(ts, str):
                return datetime.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").date() == current_time.date()
        if report_type == "week":
            start, end = _api_week_bounds(current_time)
            return (
                report.get("weeks") == weeks_label
                or (str(report.get("startTime") or "")[:10] == start[:10] and str(report.get("endTime") or "")[:10] == end[:10])
            )
        if report_type == "month":
            return str(report.get("yearmonth") or "")[:7] == current_time.strftime("%Y-%m")
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
) -> Dict[str, Any]:
    report_type = meta["report_type"]
    current_time = _parse_report_target_for_api(report_type, target_period)
    submitted = api_client.get_submitted_reports_info(report_type) or {}
    submitted_reports = submitted.get("data", []) if isinstance(submitted, dict) else []
    count = (submitted.get("flag", 0) if isinstance(submitted, dict) else 0) + 1
    title = f"第{count}天日报" if report_type == "day" else (f"第{count}周周报" if report_type == "week" else f"第{count}月月报")
    weeks_label = f"第{count}周"
    if isinstance(submitted_reports, list):
        for report in submitted_reports:
            if _same_api_report_period(report, report_type, current_time, weeks_label):
                raise HTTPException(status_code=400, detail=f"该{meta['task_name']}周期已经提交过")
    job_info = api_client.get_job_info()
    report_info = {
        "title": title,
        "content": content,
        "attachments": "",
        "reportType": report_type,
        "jobId": job_info.get("jobId", None),
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
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)
    _ensure_remote_runtime(api_client, config)
    report_type = meta["report_type"]
    submitted = api_client.get_submitted_reports_info(report_type) or {}
    count = (submitted.get("flag", 0) if isinstance(submitted, dict) else 0) + 1
    title = f"第{count}天日报" if report_type == "day" else (f"第{count}周周报" if report_type == "week" else f"第{count}月月报")
    job_info = api_client.get_job_info()
    content = ""
    if generate_content:
        ai_cfg = config.get_value("config.ai")
        if not isinstance(ai_cfg, dict):
            raise HTTPException(status_code=400, detail="未配置 AI 参数")
        if not (str(ai_cfg.get("apikey") or "").strip()) or not (str(ai_cfg.get("apiUrl") or "").strip()) or not (str(ai_cfg.get("model") or "").strip()):
            raise HTTPException(status_code=400, detail="请先在 AI 设置中填写 API URL、API Key 和 Model")
        content = generate_article(
            config,
            title,
            job_info,
            config.get_value(meta["paper_num_key"]),
            (submitted.get("data", []) if isinstance(submitted, dict) else [])[:4],
        )
    return {"config_data": config_data, "title": title, "content": content, "api_client": api_client, "config": config, "meta": meta}


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
def app_register(request: Request, req: AppRegisterRequest):
    client_ip = get_client_ip(request)
    _rate_limit(f"app_register:{client_ip}", limit=10, per_seconds=60)
    phone = (req.phone or "").strip()
    password = (req.password or "").strip()
    if not phone or len(phone) < 4:
        raise HTTPException(status_code=400, detail="请输入正确的手机号/账号")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="密码长度需为 6-100")
    with Session(engine) as session:
        exists = session.exec(select(AppUser).where(AppUser.phone == phone)).first()
        if exists:
            raise HTTPException(status_code=400, detail="该账号已注册")
        legacy = session.exec(select(User).where(User.phone == phone)).first()
        if legacy and legacy.app_password_hash:
            raise HTTPException(status_code=400, detail="该账号已注册")
        app_user = AppUser(phone=phone, password_hash=hash_password(password), enabled=True, bound_user_id=None)
        session.add(app_user)
        session.flush()
        session.add(AuditLog(actor=f"app:{app_user.id}", action="app.register", target_user_id=None, detail={}))
        session.commit()
        token = issue_token(subject=f"app:{app_user.id}", role="user")
        return {"token": token, "user_id": app_user.id, "phone": phone}

@router.post("/app/auth/login")
def app_login(request: Request, req: AppLoginRequest):
    client_ip = get_client_ip(request)
    _rate_limit(f"app_login:{client_ip}", limit=15, per_seconds=60)
    phone = (req.phone or "").strip()
    password = (req.password or "").strip()
    with Session(engine) as session:
        app_user = session.exec(select(AppUser).where(AppUser.phone == phone)).first()
        if app_user:
            if app_user.enabled is not True:
                raise HTTPException(status_code=403, detail="账号已被禁用")
            if not verify_password(password, app_user.password_hash):
                raise HTTPException(status_code=401, detail="账号或密码错误")
            token = issue_token(subject=f"app:{app_user.id}", role="user")
            session.add(AuditLog(actor=f"app:{app_user.id}", action="app.login", target_user_id=None, detail={}))
            session.commit()
            return {"token": token, "user_id": app_user.id, "phone": phone}

        legacy_user = session.exec(select(User).where(User.phone == phone)).first()
        if not legacy_user or legacy_user.app_enabled is not True or not legacy_user.app_password_hash:
            raise HTTPException(status_code=401, detail="账号或密码错误")
        if not verify_password(password, legacy_user.app_password_hash):
            raise HTTPException(status_code=401, detail="账号或密码错误")
        app_user = AppUser(
            phone=phone,
            password_hash=legacy_user.app_password_hash,
            enabled=True,
            bound_user_id=legacy_user.id,
        )
        session.add(app_user)
        session.flush()
        token = issue_token(subject=f"app:{app_user.id}", role="user")
        session.add(AuditLog(actor=f"app:{app_user.id}", action="app.login", target_user_id=legacy_user.id, detail={"legacy": True}))
        session.commit()
        return {"token": token, "user_id": app_user.id, "phone": phone}

@router.get("/app/me")
def app_me(*, session: Session = Depends(get_session), payload: dict = Depends(get_user)):
    app_user = _get_authed_app_user(session=session, payload=payload)
    if not app_user.bound_user_id:
        return AppMeResponse(app_phone=app_user.phone, bound=False, task_user=None)
    user = session.get(User, int(app_user.bound_user_id))
    if not user:
        return AppMeResponse(app_phone=app_user.phone, bound=False, task_user=None)
    return AppMeResponse(app_phone=app_user.phone, bound=True, task_user=_sanitize_user_for_self(user))

class AppMeUpdateRequest(BaseModel):
    password: Optional[str] = None
    enable_clockin: Optional[bool] = None
    clockIn: Optional[Dict[str, Any]] = None
    reportSettings: Optional[Dict[str, Any]] = None
    ai: Optional[Dict[str, Any]] = None

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

    user = session.exec(select(User).where(User.phone == task_phone)).first()
    if user:
        other = session.exec(select(AppUser).where((AppUser.bound_user_id == user.id) & (AppUser.id != app_user.id))).first()
        if other:
            raise HTTPException(status_code=400, detail="该工学云账号已被其他账号绑定")
        user.password = encrypt_secret(task_password)
        if user.enable_clockin is None:
            user.enable_clockin = True
    else:
        user = User(
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
    session.add(AuditLog(actor=str(payload.get("sub")), action="app.bind", target_user_id=user.id, detail={"task_phone": _mask_phone(task_phone)}))
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
    if req.ai is not None:
        if not isinstance(req.ai, dict):
            raise HTTPException(status_code=400, detail="ai 格式错误")
        ai_update = dict(req.ai)
        if "apikey" in ai_update and not (str(ai_update.get("apikey") or "").strip()):
            ai_update.pop("apikey", None)
        if ai_update:
            current_ai = user.ai if isinstance(user.ai, dict) else {}
            merged_ai = dict(current_ai)
            merged_ai.update(ai_update)
            user.ai = merged_ai
            changed.append("ai")
    session.add(user)
    session.add(AuditLog(actor=str(payload.get("sub")), action="app.user.update", target_user_id=user.id, detail={"fields": changed}))
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
    _rate_limit(f"app_run:{client_ip}:{user.id}", limit=3, per_seconds=60)
    config_data = user_to_config(user)
    
    specific_task_type = req.task_type if req else None
    results = run_task_by_config(
        config_data,
        specific_task_type=specific_task_type,
        force_report=bool(req.force_report) if req else False,
        target_period=req.target_period if req else None,
    )
    status = apply_execution_results_to_user(user, results, config_data)
    session.add(AuditLog(actor=str(payload.get("sub")), action="app.user.run", target_user_id=user.id, detail={"status": status}))
    session.add(user)
    session.commit()
    return {"results": results}

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
    _rate_limit(f"app_clockin_makeup:{client_ip}:{user.id}", limit=3, per_seconds=60)
    try:
        target_dates = _clockin_makeup_dates_from_request(req)
        target_type = _clockin_makeup_type_from_request(req)
        result, config_data = _makeup_clockin_for_user(user, target_dates, target_type)
        sync_runtime_fields_to_user(user, config_data)
        session.add(AuditLog(actor=str(payload.get("sub")), action="app.clockin.makeup", target_user_id=user.id, detail={"target_dates": target_dates, "target_type": target_type, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return {"ok": result.get("status") != "fail", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
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
    _rate_limit(f"app_clockin_makeup_all:{client_ip}:{user.id}", limit=2, per_seconds=60)
    try:
        target_type = _clockin_makeup_type_from_request(req or ClockInMakeupRequest())
        result, config_data, target_dates = _makeup_all_missing_clockin_for_user(user, target_type)
        sync_runtime_fields_to_user(user, config_data)
        session.add(AuditLog(actor=str(payload.get("sub")), action="app.clockin.makeup_all", target_user_id=user.id, detail={"target_dates": target_dates, "target_type": target_type, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return {"ok": result.get("status") != "fail", "result": result, "target_dates": target_dates, "target_type": target_type}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
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
        session.add(AuditLog(actor=str(payload.get("sub")), action="app.report.makeup_all", target_user_id=user.id, detail={"report_key": report_key, "target_periods": target_periods, "status": result.get("status")}))
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
    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)

    ai_cfg = config.get_value("config.ai")
    if not isinstance(ai_cfg, dict):
        raise HTTPException(status_code=400, detail="未配置 AI 参数")
    if not (str(ai_cfg.get("apikey") or "").strip()) or not (str(ai_cfg.get("apiUrl") or "").strip()) or not (str(ai_cfg.get("model") or "").strip()):
        raise HTTPException(status_code=400, detail="请先在 AI 设置中填写 API URL、API Key 和 Model")

    try:
        _ensure_remote_runtime(api_client, config)

        submitted = api_client.get_submitted_reports_info("day") or {}
        data = submitted.get("data", []) if isinstance(submitted, dict) else []
        count = (submitted.get("flag", 0) if isinstance(submitted, dict) else 0) + 1
        title = f"第{count}天日报"

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
                except Exception:
                    pass

        job_info = api_client.get_job_info()
        content = generate_article(config, title, job_info, config.get_value("planInfo.planPaper.dayPaperNum"))
        sync_runtime_fields_to_user(user, config_data)
        session.add(user)
        session.add(AuditLog(actor=str(payload.get("sub")), action="app.report.daily.generate", target_user_id=user.id, detail={"title": title, "already_submitted": already_submitted}))
        session.commit()
        return {"ok": True, "title": title, "content": content, "already_submitted": already_submitted}
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

    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)

    try:
        _ensure_remote_runtime(api_client, config)

        submitted = api_client.get_submitted_reports_info("day") or {}
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
                except Exception:
                    pass

        count = (submitted.get("flag", 0) if isinstance(submitted, dict) else 0) + 1
        title = f"第{count}天日报"
        job_info = api_client.get_job_info()
        report_info = {
            "title": title,
            "content": content,
            "attachments": "",
            "reportType": "day",
            "jobId": job_info.get("jobId", None),
            "reportTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "formFieldDtoList": api_client.get_from_info(7),
        }
        api_client.submit_report(report_info)
        sync_runtime_fields_to_user(user, config_data)
        session.add(user)
        session.add(AuditLog(actor=str(payload.get("sub")), action="app.report.daily.submit", target_user_id=user.id, detail={"title": title}))
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

_RATE_LIMIT_BUCKETS: Dict[str, List[float]] = {}
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

def _rate_limit(key: str, limit: int, per_seconds: int, detail: Optional[str] = None) -> None:
    now = time.time()
    bucket = _RATE_LIMIT_BUCKETS.get(key, [])
    cutoff = now - per_seconds
    bucket = [t for t in bucket if t >= cutoff]
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail=detail or "操作过于频繁，请稍后再试")
    bucket.append(now)
    _RATE_LIMIT_BUCKETS[key] = bucket

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

@router.post("/auth/login")
def admin_login(request: Request, req: LoginRequest):
    client_ip = get_client_ip(request)
    _rate_limit(f"login:{client_ip}", limit=10, per_seconds=60)
    username = (req.username or "").strip()
    password = (req.password or "").strip()
    with Session(engine) as session:
        user = session.exec(select(AdminUser).where(AdminUser.username == username)).first()
        if not user or not user.enabled:
            raise HTTPException(status_code=401, detail="账号或密码错误")
        if not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="账号或密码错误")
        role = user.role or "viewer"
        token = issue_token(subject=username, role=role)
        session.add(AuditLog(actor=username, action="auth.login", target_user_id=None, detail={"role": role}))
        session.commit()
        return {"token": token, "role": role, "username": username}

class AuditLogPageResponse(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    page: int
    pageSize: int

@router.get("/audit-logs/page", response_model=AuditLogPageResponse)
def read_audit_logs_page(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(get_admin),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    q: Optional[str] = Query(None, max_length=60),
):
    stmt = select(AuditLog)
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


@router.get("/settings/notifications")
def get_notification_settings(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(get_admin),
):
    return _sanitize_notification_settings_for_read(_get_notification_settings(session))


@router.patch("/settings/notifications")
def update_notification_settings(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(get_admin),
    req: NotificationSettingsUpdateRequest,
):
    settings = _save_notification_settings(session, req.smtp)
    session.add(
        AuditLog(
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
    admin: dict = Depends(get_admin),
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

@router.get("/admin-users/page", response_model=AdminUserPageResponse)
def read_admin_users_page(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(get_admin),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    q: Optional[str] = Query(None, max_length=60),
):
    stmt = select(AdminUser).where(AdminUser.role == "admin")
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
    admin: dict = Depends(get_admin),
    req: AdminUserCreateRequest,
):
    raise HTTPException(status_code=400, detail="该管理平台仅保留管理员账号，此功能已禁用")

@router.patch("/admin-users/{admin_user_id}")
def update_admin_user(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(get_admin),
    admin_user_id: int,
    req: AdminUserUpdateRequest,
):
    user = session.get(AdminUser, admin_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    changed: List[str] = []
    if user.role != "admin":
        raise HTTPException(status_code=400, detail="仅允许管理管理员账号")
    if req.role is not None:
        raise HTTPException(status_code=400, detail="不允许修改角色")
    if req.enabled is not None:
        enabled = bool(req.enabled)
        if not enabled and user.role == "admin":
            enabled_admins = session.exec(
                select(func.count()).select_from(AdminUser).where((AdminUser.role == "admin") & (AdminUser.enabled == True))
            ).one()
            if enabled_admins <= 1:
                raise HTTPException(status_code=400, detail="至少保留一个启用的管理员")
        user.enabled = enabled
        changed.append("enabled")
    session.add(user)
    session.add(AuditLog(actor=admin.get("sub"), action="admin_user.update", target_user_id=None, detail={"id": admin_user_id, "fields": changed}))
    session.commit()
    return {"ok": True}

@router.post("/admin-users/{admin_user_id}/reset-password")
def reset_admin_user_password(
    *,
    session: Session = Depends(get_session),
    admin: dict = Depends(get_admin),
    admin_user_id: int,
    req: AdminUserResetPasswordRequest,
):
    user = session.get(AdminUser, admin_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role != "admin":
        raise HTTPException(status_code=400, detail="仅允许管理管理员账号")
    password = (req.password or "").strip()
    if not password or len(password) < 6 or len(password) > 100:
        raise HTTPException(status_code=400, detail="密码长度需为 6-100")
    user.password_hash = hash_password(password)
    session.add(user)
    session.add(AuditLog(actor=admin.get("sub"), action="admin_user.reset_password", target_user_id=None, detail={"id": admin_user_id, "username": user.username}))
    session.commit()
    return {"ok": True}

@router.post("/users", response_model=UserRead)
def create_user(*, session: Session = Depends(get_session), user: UserCreate, operator: dict = Depends(get_operator)):
    db_user = User.from_orm(user)
    db_user.password = encrypt_secret(db_user.password)
    db_user.pushNotifications = normalize_push_notifications(db_user.pushNotifications)
    _ensure_clockin_schedule_defaults(db_user)
    session.add(db_user)
    session.flush()
    session.add(AuditLog(actor=operator.get("sub"), action="user.create", target_user_id=db_user.id, detail={"phone": db_user.phone}))
    session.commit()
    session.refresh(db_user)
    if db_user.enable_clockin or _any_report_enabled(db_user):
        add_user_job(db_user)
    return _sanitize_user_for_read(db_user)

@router.get("/users", response_model=List[UserRead])
def read_users(*, session: Session = Depends(get_session), admin: dict = Depends(get_admin)):
    users = session.exec(select(User)).all()
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
    viewer: dict = Depends(get_viewer),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    q: Optional[str] = Query(None, min_length=1, max_length=50),
):
    cond = None
    if q:
        qq = q.strip()
        cond = (User.phone.contains(qq)) | (func.coalesce(User.remark, "").contains(qq))

    count_stmt = select(func.count()).select_from(User)
    if cond is not None:
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
    if cond is not None:
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
def read_user(*, session: Session = Depends(get_session), user_id: int, viewer: dict = Depends(get_viewer)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _sanitize_user_for_read(user)

@router.get("/users/{user_id}/execution")
def read_user_execution(*, session: Session = Depends(get_session), user_id: int, viewer: dict = Depends(get_viewer)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"results": user.last_execution_result or []}

@router.get("/users/{user_id}/job-info")
def read_user_job_info(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    operator: dict = Depends(get_operator),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"job_info:{client_ip}:{user_id}", limit=3, per_seconds=60)
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
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
    operator: dict = Depends(get_operator),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"account_addr:{client_ip}:{user_id}", limit=3, per_seconds=60)
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
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
    operator: dict = Depends(get_operator),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"clockin_missing:{client_ip}:{user_id}", limit=10, per_seconds=60)
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
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
    operator: dict = Depends(get_operator),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"clockin_makeup:{client_ip}:{user_id}", limit=3, per_seconds=60)
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        target_dates = _clockin_makeup_dates_from_request(req)
        target_type = _clockin_makeup_type_from_request(req)
        result, config_data = _makeup_clockin_for_user(user, target_dates, target_type)
        sync_runtime_fields_to_user(user, config_data)
        session.add(AuditLog(actor=operator.get("sub"), action="user.clockin.makeup", target_user_id=user_id, detail={"target_dates": target_dates, "target_type": target_type, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return {"ok": result.get("status") != "fail", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e) or "补卡失败")


@router.post("/users/{user_id}/clock-in/makeup-all")
def makeup_all_user_clockin(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    req: Optional[ClockInMakeupRequest] = None,
    operator: dict = Depends(get_operator),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"clockin_makeup_all:{client_ip}:{user_id}", limit=2, per_seconds=60)
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        target_type = _clockin_makeup_type_from_request(req or ClockInMakeupRequest())
        result, config_data, target_dates = _makeup_all_missing_clockin_for_user(user, target_type)
        sync_runtime_fields_to_user(user, config_data)
        session.add(AuditLog(actor=operator.get("sub"), action="user.clockin.makeup_all", target_user_id=user_id, detail={"target_dates": target_dates, "target_type": target_type, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return {"ok": result.get("status") != "fail", "result": result, "target_dates": target_dates, "target_type": target_type}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e) or "全部补卡失败")

@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(*, session: Session = Depends(get_session), user_id: int, user_update: UserUpdate, operator: dict = Depends(get_operator)):
    db_user = session.get(User, user_id)
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = user_update.dict(exclude_unset=True)
    if "password" in user_data:
        user_data["password"] = encrypt_secret(str(user_data.get("password") or ""))
    if "ai" in user_data and isinstance(user_data.get("ai"), dict):
        ai_update = dict(user_data.get("ai") or {})
        if ai_update:
            current_ai = db_user.ai if isinstance(db_user.ai, dict) else {}
            merged_ai = dict(current_ai)
            merged_ai.update(ai_update)
            user_data["ai"] = merged_ai
        else:
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
    session.add(AuditLog(actor=operator.get("sub"), action="user.update", target_user_id=user_id, detail={"fields": list(user_data.keys())}))
    session.commit()
    session.refresh(db_user)

    remove_user_job(user_id)
    if db_user.enable_clockin or _any_report_enabled(db_user):
        add_user_job(db_user)

    return _sanitize_user_for_read(db_user)

@router.delete("/users/{user_id}")
def delete_user(*, session: Session = Depends(get_session), user_id: int, admin: dict = Depends(get_admin)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    remove_user_job(user_id)
    session.delete(user)
    session.add(AuditLog(actor=admin.get("sub"), action="user.delete", target_user_id=user_id, detail={}))
    session.commit()
    return {"ok": True}

@router.post("/users/{user_id}/run")
def run_user_task(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    req: Optional[AppRunRequest] = None,
    operator: dict = Depends(get_operator),
):
    client_ip = get_client_ip(request)
    _rate_limit(f"run:{client_ip}:{user_id}", limit=2, per_seconds=60)
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    config_data = user_to_config(user)
    specific_task_type = req.task_type if req else None
    results = run_task_by_config(
        config_data,
        specific_task_type=specific_task_type,
        force_report=bool(req.force_report) if req else False,
        target_period=req.target_period if req else None,
    )
    status = apply_execution_results_to_user(user, results, config_data)

    session.add(AuditLog(actor=operator.get("sub"), action="user.run", target_user_id=user_id, detail={"status": status}))
    session.add(user)
    session.commit()

    return {"results": results}

class BatchRunRequest(BaseModel):
    ids: List[int]
    concurrency: int = 5

@router.post("/users/run/batch")
def run_users_batch(*, request: Request, req: BatchRunRequest, operator: dict = Depends(get_operator)):
    client_ip = get_client_ip(request)
    _rate_limit(f"run_batch:{client_ip}", limit=1, per_seconds=10)
    ids = [int(x) for x in (req.ids or []) if int(x) > 0]
    if not ids:
        raise HTTPException(status_code=400, detail="请选择要运行的账号")
    max_concurrency = int(os.getenv("BATCH_JOB_MAX_CONCURRENCY") or "10")
    max_concurrency = max(1, min(max_concurrency, 50))
    concurrency = max(1, min(int(req.concurrency or 5), max_concurrency))
    with Session(engine) as session:
        job = BatchJob(created_by=operator.get("sub"), total=len(ids), concurrency=concurrency, user_ids=ids, status="queued")
        session.add(job)
        session.flush()
        items = [BatchJobItem(job_id=job.id, user_id=uid, status="queued") for uid in ids]
        session.add_all(items)
        session.add(AuditLog(actor=operator.get("sub"), action="batch.enqueue", target_user_id=None, detail={"job_id": job.id, "total": len(ids), "concurrency": concurrency}))
        session.commit()
        job_id = job.id
    return {"ok": True, "queued": len(ids), "concurrency": concurrency, "job_id": job_id}

@router.get("/batch-jobs/{job_id}")
def read_batch_job(*, session: Session = Depends(get_session), job_id: int = 0, viewer: dict = Depends(get_viewer)):
    job = session.get(BatchJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    running = session.exec(
        select(func.count()).select_from(BatchJobItem).where((BatchJobItem.job_id == job_id) & (BatchJobItem.status == "running"))
    ).one()
    queued = session.exec(
        select(func.count()).select_from(BatchJobItem).where((BatchJobItem.job_id == job_id) & (BatchJobItem.status == "queued"))
    ).one()
    last_errors = session.exec(
        select(BatchJobItem)
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
        "running": int(running or 0),
        "queued": int(queued or 0),
        "last_errors": [
            {"user_id": it.user_id, "message": it.error or "Fail", "ts": (it.finished_at.isoformat() if it.finished_at else None)}
            for it in last_errors
        ],
    }

@router.post("/batch-jobs/{job_id}/pause")
def pause_batch_job(*, session: Session = Depends(get_session), job_id: int, operator: dict = Depends(get_operator)):
    job = session.get(BatchJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.paused = True
    session.add(job)
    session.add(AuditLog(actor=operator.get("sub"), action="batch.pause", target_user_id=None, detail={"job_id": job_id}))
    session.commit()
    return {"ok": True}

@router.post("/batch-jobs/{job_id}/resume")
def resume_batch_job(*, session: Session = Depends(get_session), job_id: int, operator: dict = Depends(get_operator)):
    job = session.get(BatchJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.paused = False
    if job.status in ["paused", "queued"]:
        job.status = "queued"
    session.add(job)
    session.add(AuditLog(actor=operator.get("sub"), action="batch.resume", target_user_id=None, detail={"job_id": job_id}))
    session.commit()
    return {"ok": True}

@router.post("/batch-jobs/{job_id}/cancel")
def cancel_batch_job(*, session: Session = Depends(get_session), job_id: int, operator: dict = Depends(get_operator)):
    job = session.get(BatchJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.cancel_requested = True
    session.add(job)
    session.add(AuditLog(actor=operator.get("sub"), action="batch.cancel", target_user_id=None, detail={"job_id": job_id}))
    session.commit()
    return {"ok": True}

@router.post("/ai/test")
def ai_test(request: Request, req: AiTestRequest, operator: dict = Depends(get_operator)):
    client_ip = get_client_ip(request)
    _rate_limit(f"ai_test:{client_ip}", limit=5, per_seconds=60)
    api_url = (req.apiUrl or "").strip()
    api_key = (req.apikey or "").strip()
    model = (req.model or "").strip()
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
        raise HTTPException(status_code=502, detail=f"AI 接口请求失败: {str(e)}")


@router.post("/users/{user_id}/reports/{report_key}/generate")
def generate_report(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    report_key: str,
    target_period: Optional[str] = None,
    operator: dict = Depends(get_operator),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        generated = _generate_report_content_for_user(user, report_key, target_period)
        sync_runtime_fields_to_user(user, generated["config_data"])
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
    operator: dict = Depends(get_operator),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
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
    operator: dict = Depends(get_operator),
):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="报告内容不能为空")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        generated = _generate_report_content_for_user(user, report_key, req.target_period, generate_content=False)
        report_info = _build_report_info(
            api_client=generated["api_client"],
            config=generated["config"],
            meta=generated["meta"],
            content=content,
            target_period=req.target_period,
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
    operator: dict = Depends(get_operator),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        result, config_data, target_periods = _makeup_all_reports_for_user(user, report_key)
        sync_runtime_fields_to_user(user, config_data)
        session.add(AuditLog(actor=operator.get("sub"), action="user.report.makeup_all", target_user_id=user_id, detail={"report_key": report_key, "target_periods": target_periods, "status": result.get("status")}))
        session.add(user)
        session.commit()
        return {"ok": result.get("status") != "fail", "result": result, "target_periods": target_periods, "report_key": report_key}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e) or "全部补交报告失败")


@router.post("/users/{user_id}/reports/daily/generate")
def generate_daily_report(
    *,
    request: Request,
    session: Session = Depends(get_session),
    user_id: int,
    operator: dict = Depends(get_operator),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法生成日报")

    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)

    ai_cfg = config.get_value("config.ai")
    if not isinstance(ai_cfg, dict):
        raise HTTPException(status_code=400, detail="未配置 AI 参数")
    if not (str(ai_cfg.get("apikey") or "").strip()) or not (str(ai_cfg.get("apiUrl") or "").strip()) or not (str(ai_cfg.get("model") or "").strip()):
        raise HTTPException(status_code=400, detail="请先在 AI 设置中填写 API URL、API Key 和 Model")

    try:
        _ensure_remote_runtime(api_client, config)

        submitted = api_client.get_submitted_reports_info("day") or {}
        data = submitted.get("data", []) if isinstance(submitted, dict) else []
        count = (submitted.get("flag", 0) if isinstance(submitted, dict) else 0) + 1
        title = f"第{count}天日报"

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
                except Exception:
                    pass

        job_info = api_client.get_job_info()
        content = generate_article(config, title, job_info, config.get_value("planInfo.planPaper.dayPaperNum"))
        sync_runtime_fields_to_user(user, config_data)
        session.add(user)
        session.commit()
        return {"ok": True, "title": title, "content": content, "already_submitted": already_submitted}
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
    operator: dict = Depends(get_operator),
):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="日报内容不能为空")

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not (str(user.phone or "").strip()) or not (str(user.password or "").strip()):
        raise HTTPException(status_code=400, detail="该用户未保存账号或密码，无法提交日报")

    config_data = user_to_config(user)
    config = ConfigManager(config=config_data)
    api_client = ApiClient(config)

    try:
        _ensure_remote_runtime(api_client, config)

        submitted = api_client.get_submitted_reports_info("day") or {}
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
                except Exception:
                    pass

        count = (submitted.get("flag", 0) if isinstance(submitted, dict) else 0) + 1
        title = f"第{count}天日报"
        job_info = api_client.get_job_info()
        report_info = {
            "title": title,
            "content": content,
            "attachments": "",
            "reportType": "day",
            "jobId": job_info.get("jobId", None),
            "reportTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "formFieldDtoList": api_client.get_from_info(7),
        }
        api_client.submit_report(report_info)
        sync_runtime_fields_to_user(user, config_data)
        session.add(user)
        session.commit()
        return {"ok": True, "title": title, "submitted_at": report_info["reportTime"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "提交日报失败")


@router.get("/geocode/search")
def geocode_search(q: str = Query(..., min_length=1, max_length=200), operator: dict = Depends(get_operator)):
    provider = (os.getenv("GEOCODE_PROVIDER") or "").strip().lower()
    amap_key = (os.getenv("AMAP_KEY") or "").strip()
    if not provider:
        provider = "amap" if amap_key else "osm"
    q2 = (q or "").strip()
    cache_key = ("search", provider, q2)
    cached = _geocode_cache_get(cache_key)
    if cached is not None:
        return cached

    if provider == "amap" and amap_key:
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
            raise HTTPException(status_code=502, detail=f"地理搜索失败: {str(e)}")

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
    raise HTTPException(status_code=502, detail=f"地理搜索失败: {str(last_err) if last_err else 'unknown'}")


@router.get("/geocode/reverse")
def geocode_reverse(
    lat: float = Query(...),
    lon: float = Query(...),
    operator: dict = Depends(get_operator),
):
    provider = (os.getenv("GEOCODE_PROVIDER") or "").strip().lower()
    amap_key = (os.getenv("AMAP_KEY") or "").strip()
    if not provider:
        provider = "amap" if amap_key else "osm"
    cache_key = ("reverse", provider, round(float(lat), 6), round(float(lon), 6))
    cached = _geocode_cache_get(cache_key)
    if cached is not None:
        return cached

    if provider == "amap" and amap_key:
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
            raise HTTPException(status_code=502, detail=f"逆地理解析失败: {str(e)}")

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
    raise HTTPException(status_code=502, detail=f"逆地理解析失败: {str(last_err) if last_err else 'unknown'}")
