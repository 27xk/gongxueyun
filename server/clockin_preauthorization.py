import datetime
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import update
from sqlmodel import Session, select

from server.auth import issue_token, verify_token
from server.models import ClockInPreauthorization, User
from server.time_utils import utc_now


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


@dataclass(frozen=True)
class PreauthorizationClaim:
    id: int
    out_register_no: str
    target_date: datetime.date
    target_type: str


@dataclass(frozen=True)
class ClockInPreauthorizationHooks:
    tenant_id: str
    user_id: int
    db_engine: Any

    def claim(
        self,
        *,
        target_date: datetime.date,
        target_type: str,
        used_target_type: str | None = None,
    ) -> PreauthorizationClaim | None:
        return claim_preauthorization(
            self.db_engine,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            target_date=target_date,
            target_type=target_type,
            used_target_type=used_target_type,
        )

    def require_reauthorization(self, claim_id: int) -> None:
        mark_preauthorization_reauthorization_required(self.db_engine, claim_id)


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


def _business_date(value: datetime.datetime | datetime.date) -> datetime.date:
    if isinstance(value, datetime.datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=datetime.timezone.utc)
        return normalized.astimezone(BEIJING_TZ).date()
    return value


def _schedule_values(user: User) -> tuple[list[Any], str, str]:
    clock_in = user.clockIn if isinstance(user.clockIn, dict) else {}
    schedule = clock_in.get("schedule") if isinstance(clock_in.get("schedule"), dict) else {}
    weekdays = schedule.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays:
        weekdays = clock_in.get("customDays")
    if not isinstance(weekdays, list) or not weekdays:
        weekdays = [1, 2, 3, 4, 5, 6, 7]
    return (
        weekdays,
        str(schedule.get("startTime") or "07:30"),
        str(schedule.get("endTime") or "18:00"),
    )


def _rows_for_user(
    user: User,
    *,
    today: datetime.date,
) -> tuple[list[PreauthorizationRow], datetime.date, datetime.date, str, str]:
    added_date = _business_date(user.created_at)
    plan_end_date = parse_plan_end_date(user.planInfo)
    weekdays, start_time, end_time = _schedule_values(user)
    rows = build_preauthorization_rows(
        added_date=added_date,
        plan_end_date=plan_end_date,
        today=today,
        weekdays=weekdays,
        start_time=start_time,
        end_time=end_time,
    )
    return rows, added_date, plan_end_date, start_time, end_time


def validate_preauthorization_target(
    user: User,
    *,
    target_date: datetime.date,
    target_type: str,
    today: datetime.date | None = None,
) -> PreauthorizationRow:
    normalized_type = str(target_type or "").strip().upper()
    if normalized_type not in VALID_TARGET_TYPES:
        raise ValueError("预授权类型错误")
    rows, _, _, _, _ = _rows_for_user(
        user,
        today=today or datetime.datetime.now(BEIJING_TZ).date(),
    )
    for row in rows:
        if row.target_date == target_date and row.target_type == normalized_type:
            return row
    raise ValueError("目标日期或类型不在预授权列表中")


def _iso_datetime(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime.datetime) else None


def _masked_account(value: str) -> str:
    account = str(value or "")
    if len(account) <= 7:
        return "*" * len(account)
    return f"{account[:3]}{'*' * (len(account) - 7)}{account[-4:]}"


def list_preauthorizations(
    session: Session,
    user: User,
    *,
    scope: str,
    page: int,
    page_size: int,
    status: str | None = None,
    target_type: str | None = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    normalized_scope = str(scope or "future").strip().lower()
    if normalized_scope not in {"future", "past"}:
        raise ValueError("预授权列表范围错误")
    business_today = today or datetime.datetime.now(BEIJING_TZ).date()
    rows, added_date, plan_end_date, start_time, end_time = _rows_for_user(
        user,
        today=business_today,
    )
    scoped_rows = [
        row
        for row in rows
        if (row.target_type == "MAKEUP") == (normalized_scope == "past")
    ]

    records = session.exec(
        select(ClockInPreauthorization).where(
            (ClockInPreauthorization.tenant_id == user.tenant_id)
            & (ClockInPreauthorization.user_id == user.id)
            & (ClockInPreauthorization.target_date >= added_date)
            & (ClockInPreauthorization.target_date <= plan_end_date)
        )
    ).all()
    record_map = {
        (record.target_date, record.target_type): record for record in records
    }

    joined: list[dict[str, Any]] = []
    for row in scoped_rows:
        record = record_map.get((row.target_date, row.target_type))
        item_status = record.status if record else "pending"
        joined.append(
            {
                "target_date": row.target_date.isoformat(),
                "target_type": row.target_type,
                "target_time": row.target_time,
                "status": item_status,
                "authorized_at": _iso_datetime(record.authorized_at if record else None),
                "consumed_at": _iso_datetime(record.consumed_at if record else None),
                "used_target_type": record.used_target_type if record else None,
                "can_authorize": item_status != "authorized",
            }
        )

    summary_counts = Counter(item["status"] for item in joined)
    summary = {
        key: int(summary_counts.get(key, 0))
        for key in ("pending", "authorized", "consumed", "reauthorize_required")
    }
    normalized_status = str(status or "").strip().lower()
    normalized_type = str(target_type or "").strip().upper()
    filtered = [
        item
        for item in joined
        if (not normalized_status or item["status"] == normalized_status)
        and (not normalized_type or item["target_type"] == normalized_type)
    ]
    normalized_page = max(int(page or 1), 1)
    normalized_page_size = max(1, min(int(page_size or 20), 100))
    offset = (normalized_page - 1) * normalized_page_size
    return {
        "account": _masked_account(user.phone),
        "added_date": added_date.isoformat(),
        "plan_end_date": plan_end_date.isoformat(),
        "schedule": {"start_time": start_time, "end_time": end_time},
        "scope": normalized_scope,
        "summary": summary,
        "page": normalized_page,
        "page_size": normalized_page_size,
        "total": len(filtered),
        "items": filtered[offset : offset + normalized_page_size],
    }


def complete_preauthorization(
    session: Session,
    *,
    user: User,
    ticket: str,
    today: datetime.date | None = None,
) -> ClockInPreauthorization:
    if user.id is None:
        raise ValueError("预授权用户无效")
    claims = verify_registration_ticket(
        ticket,
        tenant_id=user.tenant_id,
        user_id=user.id,
    )
    target_date = datetime.date.fromisoformat(str(claims["target_date"]))
    target_type = str(claims["target_type"])
    business_today = today or datetime.datetime.now(BEIJING_TZ).date()
    rows, _, _, _, _ = _rows_for_user(user, today=business_today)
    if (target_date, target_type) not in {
        (row.target_date, row.target_type) for row in rows
    }:
        raise ValueError("目标日期或类型不在预授权列表中")

    out_register_no = str(claims["out_register_no"])
    existing = session.exec(
        select(ClockInPreauthorization).where(
            (ClockInPreauthorization.tenant_id == user.tenant_id)
            & (ClockInPreauthorization.user_id == user.id)
            & (ClockInPreauthorization.target_date == target_date)
            & (ClockInPreauthorization.target_type == target_type)
        )
    ).first()
    if existing and existing.status == "authorized":
        if existing.out_register_no == out_register_no:
            return existing
        raise ValueError("该日期已有有效预授权")
    if existing and existing.out_register_no == out_register_no:
        raise ValueError("已使用的预授权登记不能重复启用")

    now = utc_now()
    if existing is None:
        existing = ClockInPreauthorization(
            tenant_id=user.tenant_id,
            user_id=user.id,
            target_date=target_date,
            target_type=target_type,
            out_register_no=out_register_no,
            authorized_at=now,
            created_at=now,
            updated_at=now,
        )
    else:
        existing.status = "authorized"
        existing.out_register_no = out_register_no
        existing.authorized_at = now
        existing.consumed_at = None
        existing.used_target_type = None
        existing.updated_at = now
    session.add(existing)
    session.flush()
    return existing


def claim_preauthorization(
    db_engine,
    *,
    tenant_id: str,
    user_id: int,
    target_date: datetime.date,
    target_type: str,
    used_target_type: str | None = None,
) -> PreauthorizationClaim | None:
    normalized_type = str(target_type or "").strip().upper()
    normalized_used_type = str(used_target_type or "").strip().upper() or None
    if normalized_type not in VALID_TARGET_TYPES:
        raise ValueError("预授权类型错误")
    if normalized_type == "MAKEUP":
        if normalized_used_type not in {"START", "END"}:
            raise ValueError("补卡类型错误")
    elif normalized_used_type is not None:
        raise ValueError("普通打卡不能设置补卡类型")

    now = utc_now()
    with Session(db_engine) as session:
        statement = (
            update(ClockInPreauthorization)
            .where(
                (ClockInPreauthorization.tenant_id == str(tenant_id or "default"))
                & (ClockInPreauthorization.user_id == int(user_id))
                & (ClockInPreauthorization.target_date == target_date)
                & (ClockInPreauthorization.target_type == normalized_type)
                & (ClockInPreauthorization.status == "authorized")
            )
            .values(
                status="consumed",
                consumed_at=now,
                used_target_type=normalized_used_type,
                updated_at=now,
            )
        )
        result = session.exec(statement)
        if int(result.rowcount or 0) != 1:
            session.rollback()
            return None
        row = session.exec(
            select(ClockInPreauthorization).where(
                (ClockInPreauthorization.tenant_id == str(tenant_id or "default"))
                & (ClockInPreauthorization.user_id == int(user_id))
                & (ClockInPreauthorization.target_date == target_date)
                & (ClockInPreauthorization.target_type == normalized_type)
            )
        ).one()
        claim = PreauthorizationClaim(
            id=int(row.id),
            out_register_no=str(row.out_register_no),
            target_date=row.target_date,
            target_type=row.target_type,
        )
        session.commit()
        return claim


def mark_preauthorization_reauthorization_required(db_engine, claim_id: int) -> None:
    with Session(db_engine) as session:
        session.exec(
            update(ClockInPreauthorization)
            .where(
                (ClockInPreauthorization.id == int(claim_id))
                & (ClockInPreauthorization.status == "consumed")
            )
            .values(status="reauthorize_required", updated_at=utc_now())
        )
        session.commit()


def build_preauthorization_hooks(
    user: User,
    *,
    db_engine,
) -> ClockInPreauthorizationHooks:
    if user.id is None:
        raise ValueError("预授权用户无效")
    return ClockInPreauthorizationHooks(
        tenant_id=str(user.tenant_id or "default"),
        user_id=int(user.id),
        db_engine=db_engine,
    )
