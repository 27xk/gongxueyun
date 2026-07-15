import logging
import datetime
import os
import time
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from server.task_runner import run_task_by_config
from server.clockin_preauthorization import build_preauthorization_hooks
from server.database import engine
from server.models import DEFAULT_TENANT_ID, Tenant, User
from server.user_runtime import apply_execution_results_to_user, user_to_config as build_user_config
from sqlmodel import Session, select
from typing import Dict, Any
from server.execution_locks import acquire_task_lock, release_task_lock
from server.observability import record_task_event
from server.security import int_env

def _resolve_scheduler_timezone():
    tz_name = (os.getenv("SCHEDULER_TIMEZONE") or os.getenv("TZ") or "").strip()
    if not tz_name:
        tz_name = "Asia/Shanghai"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None

scheduler = BackgroundScheduler(timezone=_resolve_scheduler_timezone())
logger = logging.getLogger(__name__)


def _task_lock_ttl_seconds() -> int:
    try:
        value = int(os.getenv("TASK_LOCK_TTL_SECONDS") or "1800")
    except Exception:
        value = 1800
    return max(60, min(value, 24 * 60 * 60))

def user_to_config(user: User) -> Dict[str, Any]:
    return build_user_config(user)

def _weekday_list_to_cron(weekdays):
    mapping = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}
    if not isinstance(weekdays, list) or len(weekdays) == 0:
        return None
    parts = []
    for d in weekdays:
        try:
            d_int = int(d)
        except Exception:
            continue
        if d_int in mapping:
            parts.append(mapping[d_int])
    return ",".join(sorted(set(parts))) if parts else None


def _parse_hhmm(value: Any, default_h: int, default_m: int):
    if not isinstance(value, str) or ":" not in value:
        return default_h, default_m
    try:
        hh, mm = value.split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return default_h, default_m


def _get_schedule(user: User) -> Dict[str, Any]:
    clock_in = user.clockIn or {}
    schedule = (clock_in.get("schedule") or {}) if isinstance(clock_in, dict) else {}
    start_time = schedule.get("startTime") or "07:30"
    end_time = schedule.get("endTime") or "18:00"
    weekdays = schedule.get("weekdays")
    if weekdays is None:
        weekdays = clock_in.get("customDays") if isinstance(clock_in, dict) else None
    if weekdays is None:
        weekdays = [1, 2, 3, 4, 5, 6, 7]
    total_days = schedule.get("totalDays") if isinstance(schedule, dict) else None
    start_date = schedule.get("startDate") if isinstance(schedule, dict) else None
    return {
        "startTime": start_time,
        "endTime": end_time,
        "weekdays": weekdays,
        "totalDays": total_days,
        "startDate": start_date,
    }


def run_job(user_id: int, forced_checkin_type: str):
    with Session(engine) as session:
        user = session.get(User, user_id)
        if (
            not user
            or getattr(user, "deleted_at", None) is not None
            or not user.enable_clockin
            or not _is_user_tenant_active(session, user)
        ):
            logger.info(f"用户 {user_id} 不存在或已禁用，跳过任务")
            return

        schedule = _get_schedule(user)
        total_days = schedule.get("totalDays")
        start_date = schedule.get("startDate")
        if isinstance(total_days, int) and total_days > 0:
            if not start_date:
                start_date = datetime.date.today().strftime("%Y-%m-%d")
                if isinstance(user.clockIn, dict):
                    user.clockIn.setdefault("schedule", {})["startDate"] = start_date
                    session.add(user)
                    session.commit()
            try:
                start_dt = datetime.datetime.strptime(str(start_date), "%Y-%m-%d").date()
                if datetime.date.today() >= (start_dt + datetime.timedelta(days=total_days)):
                    user.enable_clockin = False
                    session.add(user)
                    session.commit()
                    remove_user_job(user_id)
                    logger.info(f"用户 {user_id} 打卡天数已到期，已自动停用")
                    return
            except Exception as exc:
                logger.warning("invalid schedule startDate for user %s: %r (%s)", user_id, start_date, exc)
            
        config_data = user_to_config(user)
        preauthorization_hooks = build_preauthorization_hooks(user, db_engine=engine)

    lock_key = f"scheduler:user:{user_id}:clock_in:{forced_checkin_type}"
    lock_token = acquire_task_lock(
        lock_key,
        ttl_seconds=_task_lock_ttl_seconds(),
        detail={"user_id": user_id, "forced_checkin_type": forced_checkin_type},
    )
    if lock_token is None:
        logger.info("scheduler job already running: %s", lock_key)
        record_task_event(
            source="scheduler",
            event="skip_locked",
            task_key=lock_key,
            user_id=user_id,
            status="locked",
        )
        return
        
    logger.info(f"开始执行用户 {user_id} 的定时任务")
    started = time.monotonic()
    status = "success"
    error = None
    try:
        results = run_task_by_config(
            config_data,
            forced_checkin_type=forced_checkin_type,
            preauthorization_hooks=preauthorization_hooks,
        )
        
        # 更新状态
        with Session(engine) as session:
            user = session.get(User, user_id)
            if user and getattr(user, "deleted_at", None) is None:
                apply_execution_results_to_user(user, results, config_data)
                session.add(user)
                session.commit()
    except Exception as e:
        status = "fail"
        error = str(e)
        logger.error(f"任务执行异常: {e}")

    finally:
        record_task_event(
            source="scheduler",
            event="finish",
            task_key=lock_key,
            user_id=user_id,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )
        release_task_lock(lock_token)


def _get_report_settings(user: User) -> Dict[str, Any]:
    rs = user.reportSettings or {}
    if not isinstance(rs, dict):
        rs = {}
    daily = rs.get("daily") if isinstance(rs.get("daily"), dict) else {}
    weekly = rs.get("weekly") if isinstance(rs.get("weekly"), dict) else {}
    monthly = rs.get("monthly") if isinstance(rs.get("monthly"), dict) else {}
    return {"daily": daily, "weekly": weekly, "monthly": monthly}

def _parse_hhmm_str(value: Any, default_h: int, default_m: int):
    return _parse_hhmm(value, default_h, default_m)

def run_report_job(user_id: int, specific_task_type: str):
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user or getattr(user, "deleted_at", None) is not None or not _is_user_tenant_active(session, user):
            logger.info(f"用户 {user_id} 不存在，跳过任务")
            return
        if not _is_report_task_enabled(user, specific_task_type):
            logger.info("用户 %s 未启用报告任务 %s，跳过任务", user_id, specific_task_type)
            return
        config_data = user_to_config(user)

    lock_key = f"scheduler:user:{user_id}:report:{specific_task_type}"
    lock_token = acquire_task_lock(
        lock_key,
        ttl_seconds=_task_lock_ttl_seconds(),
        detail={"user_id": user_id, "specific_task_type": specific_task_type},
    )
    if lock_token is None:
        logger.info("scheduler report job already running: %s", lock_key)
        record_task_event(
            source="scheduler",
            event="skip_locked",
            task_key=lock_key,
            user_id=user_id,
            status="locked",
        )
        return

    logger.info(f"开始执行用户 {user_id} 的定时报告任务: {specific_task_type}")
    started = time.monotonic()
    status = "success"
    error = None
    try:
        results = run_task_by_config(config_data, specific_task_type=specific_task_type)
        with Session(engine) as session:
            user = session.get(User, user_id)
            if user and getattr(user, "deleted_at", None) is None:
                apply_execution_results_to_user(user, results, config_data)
                session.add(user)
                session.commit()
    except Exception as e:
        logger.error(f"任务执行异常: {e}")

        status = "fail"
        error = str(e)
    finally:
        record_task_event(
            source="scheduler",
            event="finish",
            task_key=lock_key,
            user_id=user_id,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )
        release_task_lock(lock_token)


def add_user_job(user: User):
    schedule = _get_schedule(user)
    start_h, start_m = _parse_hhmm(schedule.get("startTime"), 7, 30)
    end_h, end_m = _parse_hhmm(schedule.get("endTime"), 18, 0)
    dow = _weekday_list_to_cron(schedule.get("weekdays"))
    if not dow:
        return
    try:
        jitter_seconds = int(os.getenv("SCHEDULER_JITTER_SECONDS") or "600")
    except Exception:
        jitter_seconds = 600
    jitter_seconds = max(0, min(jitter_seconds, 3600))
    try:
        report_jitter_seconds = int(os.getenv("SCHEDULER_REPORT_JITTER_SECONDS") or "0")
    except Exception:
        report_jitter_seconds = 0
    report_jitter_seconds = max(0, min(report_jitter_seconds, 3600))

    if user.enable_clockin:
        scheduler.add_job(
            run_job,
            CronTrigger(day_of_week=dow, hour=start_h, minute=start_m, jitter=jitter_seconds),
            args=[user.id, "START"],
            id=f"user_{user.id}_start",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=15 * 60,
        )
        scheduler.add_job(
            run_job,
            CronTrigger(day_of_week=dow, hour=end_h, minute=end_m, jitter=jitter_seconds),
            args=[user.id, "END"],
            id=f"user_{user.id}_end",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=15 * 60,
        )

    rs = _get_report_settings(user)
    daily = rs.get("daily") or {}
    weekly = rs.get("weekly") or {}
    monthly = rs.get("monthly") or {}

    if daily.get("enabled") is True:
        submit_time = daily.get("submitTime") or "12:00"
        submit_days = daily.get("submitDays")
        daily_dow = _weekday_list_to_cron(submit_days) if submit_days is not None else "mon,tue,wed,thu,fri,sat,sun"
        if daily_dow:
            hh, mm = _parse_hhmm_str(submit_time, 12, 0)
            scheduler.add_job(
                run_report_job,
                CronTrigger(day_of_week=daily_dow, hour=hh, minute=mm, jitter=report_jitter_seconds),
                args=[user.id, "daily_report"],
                id=f"user_{user.id}_daily_report",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=60 * 60,
            )

    if weekly.get("enabled") is True:
        try:
            submit_weekday = int(weekly.get("submitTime"))
        except Exception:
            submit_weekday = 1
        weekly_dow = _weekday_list_to_cron([submit_weekday])
        submit_at = weekly.get("submitAt") or "12:00"
        hh, mm = _parse_hhmm_str(submit_at, 12, 0)
        if weekly_dow:
            scheduler.add_job(
                run_report_job,
                CronTrigger(day_of_week=weekly_dow, hour=hh, minute=mm, jitter=report_jitter_seconds),
                args=[user.id, "weekly_report"],
                id=f"user_{user.id}_weekly_report",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=12 * 60 * 60,
            )

    if monthly.get("enabled") is True:
        try:
            submit_day = int(monthly.get("submitTime"))
        except Exception:
            submit_day = 20
        submit_day = max(1, min(submit_day, 31))
        day_expr = "28,29,30,31" if submit_day >= 28 else str(submit_day)
        submit_at = monthly.get("submitAt") or "12:00"
        hh, mm = _parse_hhmm_str(submit_at, 12, 0)
        scheduler.add_job(
            run_report_job,
            CronTrigger(day=day_expr, hour=hh, minute=mm, jitter=report_jitter_seconds),
            args=[user.id, "monthly_report"],
            id=f"user_{user.id}_monthly_report",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=24 * 60 * 60,
        )

def remove_user_job(user_id: int):
    for suffix in ("start", "end", "daily_report", "weekly_report", "monthly_report"):
        job_id = f"user_{user_id}_{suffix}"
        try:
            scheduler.remove_job(job_id)
        except Exception as exc:
            logger.debug("scheduler job removal skipped for %s: %s", job_id, exc)


def _has_enabled_report(user: User) -> bool:
    settings = user.reportSettings if isinstance(user.reportSettings, dict) else {}
    for key in ("daily", "weekly", "monthly"):
        value = settings.get(key)
        if isinstance(value, dict) and value.get("enabled") is True:
            return True
    return False


def _report_key_from_task_type(task_type: str) -> str:
    normalized = str(task_type or "").strip()
    if normalized.endswith("_report"):
        normalized = normalized[: -len("_report")]
    return normalized


def _is_report_task_enabled(user: User, task_type: str) -> bool:
    key = _report_key_from_task_type(task_type)
    if key == "report":
        return _has_enabled_report(user)
    if key not in {"daily", "weekly", "monthly"}:
        return False
    settings = _get_report_settings(user).get(key) or {}
    return isinstance(settings, dict) and settings.get("enabled") is True


def _has_scheduled_work(user: User) -> bool:
    return bool(user.enable_clockin or _has_enabled_report(user))


def _is_tenant_active(session: Session, tenant_id: str | None) -> bool:
    normalized = str(tenant_id or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID
    tenant = session.get(Tenant, normalized)
    if tenant is None:
        return normalized == DEFAULT_TENANT_ID
    return str(tenant.status or "active").lower() == "active"


def _is_user_tenant_active(session: Session, user: User) -> bool:
    return _is_tenant_active(session, getattr(user, "tenant_id", DEFAULT_TENANT_ID))


def _iter_schedulable_users(session: Session, page_size: int | None = None):
    size = int(page_size or int_env("SCHEDULER_LOAD_PAGE_SIZE", 500, min_value=50, max_value=5000))
    last_id = 0
    while True:
        rows = session.exec(
            select(User)
            .where((User.deleted_at.is_(None)) & (User.id > last_id))
            .order_by(User.id)
            .limit(size)
        ).all()
        if not rows:
            break
        last_id = int(rows[-1].id or last_id)
        for user in rows:
            if _is_user_tenant_active(session, user) and _has_scheduled_work(user):
                yield user


def start_scheduler():
    if scheduler.running:
        return
    scheduler.start()
    with Session(engine) as session:
        # 注意：这里可能需要处理数据库还没初始化的问题，最好在 main.py 里先调 create_db_and_tables
        try:
            loaded = 0
            for user in _iter_schedulable_users(session):
                add_user_job(user)
                loaded += 1
            logger.info(f"调度器启动，加载了 {loaded} 个用户的任务")
        except Exception as e:
            logger.error(f"加载任务失败（可能是数据库未初始化）: {e}")


def is_scheduler_running() -> bool:
    return bool(scheduler.running)
