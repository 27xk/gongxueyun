import datetime
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from server.auth import issue_token, verify_token


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
VALID_TARGET_TYPES = {"START", "END", "MAKEUP"}
REGISTRATION_TICKET_PURPOSE = "clockin_preauthorization"
REGISTRATION_TICKET_ROLE = "clockin_preauthorization"
REGISTRATION_TICKET_TTL_SECONDS = 30 * 60


@dataclass(frozen=True)
class PreauthorizationRow:
    target_date: datetime.date
    target_type: str
    target_time: str | None


def parse_plan_end_date(plan_info: dict[str, Any] | None) -> datetime.date:
    value = (plan_info or {}).get("endTime")
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    raw = str(value or "").strip().replace("/", "-")
    try:
        return datetime.datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError("无法获取实习计划结束时间") from exc


def build_preauthorization_rows(
    *,
    added_date: datetime.date,
    plan_end_date: datetime.date,
    today: datetime.date,
    weekdays: list[int],
    start_time: str,
    end_time: str,
) -> list[PreauthorizationRow]:
    if added_date > plan_end_date:
        return []
    enabled_weekdays: set[int] = set()
    for value in weekdays:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= normalized <= 7:
            enabled_weekdays.add(normalized)
    rows: list[PreauthorizationRow] = []
    current = added_date
    while current <= plan_end_date:
        if current.weekday() + 1 in enabled_weekdays:
            if current < today:
                rows.append(PreauthorizationRow(current, "MAKEUP", None))
            else:
                rows.append(PreauthorizationRow(current, "START", str(start_time or "")))
                rows.append(PreauthorizationRow(current, "END", str(end_time or "")))
        current += datetime.timedelta(days=1)
    return rows


def _beijing_time(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ)


def build_alipay_open_urls(
    register_url: str,
    *,
    account: str,
    started_at: datetime.datetime,
) -> tuple[str, str]:
    parsed = urlsplit(str(register_url or "").strip())
    if parsed.scheme.lower() != "alipays" or not parsed.netloc:
        raise ValueError("支付宝安全验证链接无效")

    started_text = _beijing_time(started_at).strftime("%Y-%m-%d %H:%M:%S")
    callback_text = (
        "你已经成功了，请返回点击我已完成授权，"
        f"本次授权账号：{account}，"
        f"本次授权时间：{started_text}"
    )
    callback_url = urlunsplit(
        (
            "https",
            "fanyi.baidu.com",
            "/m/trans",
            urlencode({"from": "zh", "to": "en", "query": callback_text}),
            "",
        )
    )

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "thirdpartschema"
    ]
    query_pairs.append(("thirdPartSchema", callback_url))
    direct_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query_pairs), parsed.fragment)
    )
    browser_url = urlunsplit(
        (
            "https",
            "ds.alipay.com",
            "/",
            urlencode({"scheme": direct_url}),
            "",
        )
    )
    return direct_url, browser_url


def issue_registration_ticket(
    *,
    tenant_id: str,
    user_id: int,
    target_date: datetime.date,
    target_type: str,
    out_register_no: str,
    started_at: datetime.datetime,
    ttl_seconds: int = REGISTRATION_TICKET_TTL_SECONDS,
) -> str:
    normalized_type = str(target_type or "").strip().upper()
    normalized_register_no = str(out_register_no or "").strip()
    if normalized_type not in VALID_TARGET_TYPES or not normalized_register_no:
        raise ValueError("预授权登记信息无效")
    return issue_token(
        subject=f"preauthorization:{int(user_id)}",
        role=REGISTRATION_TICKET_ROLE,
        ttl_seconds=ttl_seconds,
        tenant_id=str(tenant_id or "default"),
        extra_claims={
            "purpose": REGISTRATION_TICKET_PURPOSE,
            "user_id": int(user_id),
            "target_date": target_date.isoformat(),
            "target_type": normalized_type,
            "out_register_no": normalized_register_no,
            "started_at": started_at.isoformat(),
        },
    )


def verify_registration_ticket(
    ticket: str,
    *,
    tenant_id: str,
    user_id: int,
) -> dict[str, Any]:
    try:
        claims = verify_token(str(ticket or "").strip())
    except HTTPException as exc:
        raise ValueError("预授权登记已过期或无效") from exc

    expected_tenant = str(tenant_id or "default")
    if (
        claims.get("purpose") != REGISTRATION_TICKET_PURPOSE
        or claims.get("role") != REGISTRATION_TICKET_ROLE
        or claims.get("sub") != f"preauthorization:{int(user_id)}"
        or str(claims.get("tenant_id") or "") != expected_tenant
        or int(claims.get("user_id") or 0) != int(user_id)
    ):
        raise ValueError("预授权登记归属无效")

    target_type = str(claims.get("target_type") or "").upper()
    try:
        datetime.date.fromisoformat(str(claims.get("target_date") or ""))
        datetime.datetime.fromisoformat(str(claims.get("started_at") or ""))
    except Exception as exc:
        raise ValueError("预授权登记内容无效") from exc
    if target_type not in VALID_TARGET_TYPES or not str(
        claims.get("out_register_no") or ""
    ).strip():
        raise ValueError("预授权登记内容无效")
    return claims
