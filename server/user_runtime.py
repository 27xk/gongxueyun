import copy
import datetime
import time
from typing import Any, Callable, Dict, List

from server.secret_store import decrypt_secret

SERVER_PUSH_TYPE = "Server"
SMTP_PUSH_TYPE = "SMTP"
SUPPORTED_PUSH_TYPES = [SERVER_PUSH_TYPE, SMTP_PUSH_TYPE]
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
DEFAULT_SMTP_FROM = "工学云签到通知"


def _json_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def runtime_login_valid(user_info: Dict[str, Any] | None, now_ms: int | None = None) -> bool:
    info = _as_dict(user_info)
    token = str(info.get("token") or "").strip()
    if not token:
        return False

    expired_raw = info.get("expiredTime")
    if expired_raw in [None, ""]:
        return True

    try:
        expired_ms = int(expired_raw)
    except Exception:
        return True

    if expired_ms < 10_000_000_000:
        expired_ms *= 1000

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return expired_ms > int(now_ms)


def runtime_plan_required(config_data: Dict[str, Any] | None) -> bool:
    data = _as_dict(config_data)
    user_info = _as_dict(data.get("userInfo"))
    if str(user_info.get("userType") or "").strip().lower() == "teacher":
        return False
    plan_info = _as_dict(data.get("planInfo"))
    return not str(plan_info.get("planId") or "").strip()


def _collect_push_items(push_notifications: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in _as_list(push_notifications):
        if not isinstance(item, dict):
            continue
        push_type = str(item.get("type") or "").strip()
        if push_type not in SUPPORTED_PUSH_TYPES:
            continue
        out[push_type] = dict(item)
    return out


def normalize_push_notifications(
    push_notifications: Any,
    existing: Any = None,
    *,
    redact_secrets: bool = False,
) -> List[Dict[str, Any]]:
    incoming_map = _collect_push_items(push_notifications)
    existing_map = _collect_push_items(existing)

    incoming_server = incoming_map.get(SERVER_PUSH_TYPE, {})
    existing_server = existing_map.get(SERVER_PUSH_TYPE, {})
    send_key = str(
        incoming_server.get("sendKey")
        if "sendKey" in incoming_server
        else existing_server.get("sendKey", "")
    ).strip()
    server_item = {
        "type": SERVER_PUSH_TYPE,
        "enabled": bool(
            incoming_server["enabled"]
            if "enabled" in incoming_server
            else existing_server.get("enabled", False)
        ),
        "sendKey": "" if redact_secrets else send_key,
    }

    incoming_smtp = incoming_map.get(SMTP_PUSH_TYPE, {})
    existing_smtp = existing_map.get(SMTP_PUSH_TYPE, {})
    smtp_item = {
        "type": SMTP_PUSH_TYPE,
        "enabled": bool(
            incoming_smtp["enabled"]
            if "enabled" in incoming_smtp
            else existing_smtp.get("enabled", False)
        ),
        "to": str(
            incoming_smtp.get("to")
            if "to" in incoming_smtp
            else existing_smtp.get("to", "")
        ).strip(),
    }

    return [server_item, smtp_item]


def normalize_smtp_settings(
    settings: Any,
    existing: Any = None,
    *,
    redact_secrets: bool = False,
) -> Dict[str, Any]:
    incoming = _as_dict(settings)
    current = _as_dict(existing)
    password = str(
        incoming.get("password")
        if "password" in incoming
        else current.get("password", "")
    ).strip()
    return {
        "enabled": bool(incoming["enabled"] if "enabled" in incoming else current.get("enabled", False)),
        "host": SMTP_HOST,
        "port": SMTP_PORT,
        "username": str(incoming.get("username") if "username" in incoming else current.get("username", "")).strip(),
        "password": "" if redact_secrets else password,
        "from": str(incoming.get("from") if "from" in incoming else current.get("from", DEFAULT_SMTP_FROM)).strip() or DEFAULT_SMTP_FROM,
    }


def build_effective_push_notifications(
    *,
    user_push: Any,
    smtp_settings: Any,
) -> List[Dict[str, Any]]:
    user_items = normalize_push_notifications(user_push)
    user_map = _collect_push_items(user_items)
    server_item = user_map.get(SERVER_PUSH_TYPE, {"type": SERVER_PUSH_TYPE, "enabled": False, "sendKey": ""})
    user_smtp = user_map.get(SMTP_PUSH_TYPE, {"type": SMTP_PUSH_TYPE, "enabled": False, "to": ""})
    global_smtp = normalize_smtp_settings(smtp_settings)
    smtp_item = {
        "type": SMTP_PUSH_TYPE,
        "enabled": bool(global_smtp.get("enabled")) and bool(user_smtp.get("enabled")),
        "host": SMTP_HOST,
        "port": SMTP_PORT,
        "username": str(global_smtp.get("username") or "").strip(),
        "password": str(global_smtp.get("password") or "").strip(),
        "from": str(global_smtp.get("from") or DEFAULT_SMTP_FROM).strip() or DEFAULT_SMTP_FROM,
        "to": str(user_smtp.get("to") or "").strip(),
    }
    return [server_item, smtp_item]


def user_to_config(
    user: Any,
    *,
    decrypt_password: Callable[[str], str] = decrypt_secret,
) -> Dict[str, Any]:
    try:
        password = decrypt_password(getattr(user, "password", ""))
    except Exception:
        password = ""

    return {
        "config": {
            "user": {"phone": getattr(user, "phone", ""), "password": password},
            "clockIn": _json_copy(getattr(user, "clockIn", {}) or {}),
            "reportSettings": _json_copy(getattr(user, "reportSettings", {}) or {}),
            "ai": _json_copy(getattr(user, "ai", {}) or {}),
            "pushNotifications": normalize_push_notifications(getattr(user, "pushNotifications", []) or []),
            "device": getattr(user, "device", ""),
        },
        "userInfo": _json_copy(getattr(user, "userInfo", {}) or {}),
        "planInfo": _json_copy(getattr(user, "planInfo", {}) or {}),
    }


def sync_runtime_fields_to_user(user: Any, config_data: Dict[str, Any] | None) -> bool:
    data = _as_dict(config_data)
    changed = False

    new_user_info = _json_copy(_as_dict(data.get("userInfo")))
    new_plan_info = _json_copy(_as_dict(data.get("planInfo")))
    new_push = normalize_push_notifications(
        _as_dict(data.get("config")).get("pushNotifications"),
        getattr(user, "pushNotifications", []),
    )

    if _as_dict(getattr(user, "userInfo", {})) != new_user_info:
        user.userInfo = new_user_info
        changed = True
    if _as_dict(getattr(user, "planInfo", {})) != new_plan_info:
        user.planInfo = new_plan_info
        changed = True
    if normalize_push_notifications(getattr(user, "pushNotifications", [])) != new_push:
        user.pushNotifications = new_push
        changed = True

    return changed



def apply_execution_results_to_user(
    user: Any,
    results: List[Dict[str, Any]] | None,
    config_data: Dict[str, Any] | None,
    now: datetime.datetime | None = None,
) -> str:
    now = now or datetime.datetime.now()
    items = results if isinstance(results, list) else []

    user.last_run_time = now.strftime("%Y-%m-%d %H:%M:%S")

    status = "Success"
    for item in items:
        if item.get("status") == "fail":
            status = "Fail"
            break
    user.last_status = status

    log_summary = []
    for item in items:
        if item.get("status") != "skip":
            log_summary.append(f"{item.get('task_type')}: {item.get('message')}")
    if log_summary:
        user.logs = log_summary

    user.last_execution_result = items
    sync_runtime_fields_to_user(user, config_data)
    return status
