import logging
import os
import json
import random
import threading
import time
from datetime import datetime, timedelta, time as datetime_time
from typing import Dict, List, Optional, Any, Callable

from server.coreApi.MainLogicApi import ApiClient
from server.coreApi.AiServiceClient import generate_article
from server.clockin_backfill import (
    CLOCKIN_TYPE_LABELS,
    combine_date_time,
    normalize_clockin_records,
    parse_clockin_date,
)
from server.database import engine
from server.models import SystemSetting
from server.util.Config import ConfigManager
from server.util.MessagePush import MessagePusher
from server.user_runtime import (
    build_effective_push_notifications,
    normalize_smtp_settings,
    runtime_login_valid,
    runtime_plan_required,
)
from server.util.HelperFunctions import desensitize_name, is_holiday
from server.util.FileUploader import upload_img
from server.util.LoggerContext import _log_ctx
from sqlmodel import Session

logger = logging.getLogger("server.task_runner")

DEFAULT_CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS = 2.0
MAX_CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS = 30.0
DEFAULT_CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES = 3
DEFAULT_CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS = 10.0
MAX_CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS = 120.0


def _clockin_makeup_batch_delay_seconds() -> float:
    raw = os.getenv("CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS", str(DEFAULT_CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS))
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS
    if value < 0:
        return 0.0
    return min(value, MAX_CLOCKIN_MAKEUP_BATCH_DELAY_SECONDS)


def _clockin_makeup_rate_limit_retries() -> int:
    raw = os.getenv("CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES", str(DEFAULT_CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES))
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_CLOCKIN_MAKEUP_RATE_LIMIT_RETRIES
    return max(value, 0)


def _clockin_makeup_rate_limit_retry_seconds() -> float:
    raw = os.getenv("CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS", str(DEFAULT_CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS))
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS
    if value < 0:
        return 0.0
    return min(value, MAX_CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS)


def _is_clockin_rate_limited(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict) or result.get("status") != "fail":
        return False
    text = f"{result.get('message') or ''} {json.dumps(result.get('details') or {}, ensure_ascii=False)}"
    patterns = ("请求过于频繁", "操作过于频繁", "IP请求过于频繁", "429", "too many requests", "rate limit")
    lower = text.lower()
    return any(item.lower() in lower for item in patterns)


def _with_makeup_retry_details(result: Dict[str, Any], retries: int, retry_wait_seconds: float) -> Dict[str, Any]:
    if retries <= 0:
        return result
    details = result.get("details")
    if not isinstance(details, dict):
        details = {}
    result["details"] = {
        **details,
        "频繁重试次数": retries,
        "频繁重试等待秒": retry_wait_seconds,
    }
    return result


def _perform_clock_in_makeup_with_rate_limit_retry(
    api_client: ApiClient,
    config: ConfigManager,
    target_date: str,
    target_type: Optional[str],
    retry_count: int,
    retry_seconds: float,
) -> tuple[Dict[str, Any], int]:
    retries_used = 0
    while True:
        result = perform_clock_in_makeup(api_client, config, target_date, target_type=target_type)
        if not _is_clockin_rate_limited(result) or retries_used >= retry_count:
            return _with_makeup_retry_details(result, retries_used, retry_seconds), retries_used
        wait_seconds = min(retry_seconds * (2 ** retries_used), MAX_CLOCKIN_MAKEUP_RATE_LIMIT_RETRY_SECONDS)
        logger.warning("补卡触发频繁请求限制，等待 %.1f 秒后重试 %s", wait_seconds, target_date)
        time.sleep(wait_seconds)
        retries_used += 1


def _month_bounds(dt: datetime) -> tuple[datetime, datetime]:
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(days=1)
    return start, end


def _parse_report_target(report_type: str, target_period: Optional[str]) -> Optional[datetime]:
    if not target_period:
        return None
    raw = str(target_period).strip()
    try:
        if report_type == "month":
            return datetime.strptime(raw[:7], "%Y-%m")
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except Exception:
        return None


def _week_bounds(dt: datetime) -> tuple[str, str]:
    start = dt - timedelta(days=dt.weekday())
    end = start + timedelta(days=6)
    return start.strftime("%Y-%m-%d 00:00:00"), end.strftime("%Y-%m-%d 23:59:59")


def _same_report_period(report: Dict[str, Any], report_type: str, current_time: datetime, weeks_label: str) -> bool:
    try:
        if report_type == "day":
            ts = report.get("createTime") or report.get("reportTime")
            if isinstance(ts, str):
                return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").date() == current_time.date()
        if report_type == "week":
            start, end = _week_bounds(current_time)
            return (
                report.get("weeks") == weeks_label
                or (str(report.get("startTime") or "")[:10] == start[:10] and str(report.get("endTime") or "")[:10] == end[:10])
            )
        if report_type == "month":
            return str(report.get("yearmonth") or "")[:7] == current_time.strftime("%Y-%m")
    except Exception:
        return False
    return False


def _load_global_smtp_settings() -> Dict[str, Any]:
    with Session(engine) as session:
        row = session.get(SystemSetting, "notifications")
        value = row.value if row and isinstance(row.value, dict) else {}
    return normalize_smtp_settings((value or {}).get("smtp"))

def perform_clock_in(
    api_client: ApiClient,
    config: ConfigManager,
    forced_checkin_type: Optional[str] = None,
    target_time: Optional[datetime] = None,
    replace: bool = False,
) -> Dict[str, Any]:
    """执行打卡操作"""
    try:
        current_time = target_time or datetime.now()
        current_hour = current_time.hour
        address = config.get_value("config.clockIn.location.address")

        # 确定打卡类型
        if forced_checkin_type in ("START", "END"):
            checkin_type = forced_checkin_type
            display_type = CLOCKIN_TYPE_LABELS.get(checkin_type, checkin_type)
        else:
            if current_hour < 12:
                checkin_type = "START"
            else:
                checkin_type = "END"
            display_type = CLOCKIN_TYPE_LABELS.get(checkin_type, checkin_type)

        # 检查配置：是否跳过节假日/自定义日期
        clock_in_mode = config.get_value("config.clockIn.mode")
        special_clock_in = config.get_value("config.clockIn.specialClockIn")

        should_skip = False
        skip_message = ""

        if not replace and clock_in_mode == "holiday" and is_holiday(current_time):
            if not special_clock_in:
                should_skip = True
                skip_message = "今天是休息日，已跳过打卡"
            else:
                checkin_type = "HOLIDAY"
                display_type = "休息/节假日"

        elif not replace and clock_in_mode == "custom":
            today_weekday = current_time.weekday() + 1
            custom_days = config.get_value("config.clockIn.customDays") or []
            if today_weekday not in custom_days:
                if not special_clock_in:
                    should_skip = True
                    skip_message = "今天不在设置打卡时间范围内，已跳过打卡"
                else:
                    checkin_type = "HOLIDAY"
                    display_type = "休息/节假日"

        if should_skip:
            return {
                "status": "skip",
                "message": skip_message,
                "task_type": "打卡",
                "details": {
                    "打卡类型": display_type,
                    "打卡时间": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "打卡地点": address,
                },
            }

        start_time, end_time = _month_bounds(current_time)
        checkin_records = api_client.get_checkin_records(start_time, end_time)
        last_checkin_info = checkin_records[0] if checkin_records else {}

        # 检查是否已经打过卡
        record_date = current_time.strftime("%Y-%m-%d")
        for item in normalize_clockin_records(checkin_records):
            if item["date"] == record_date and item["type"] == checkin_type:
                logger.info(f"{record_date} {display_type} 卡已打，无需重复打卡")
                return {
                    "status": "skip",
                    "message": f"{record_date} {display_type} 卡已打，无需重复打卡",
                    "task_type": "打卡",
                    "details": {
                        "打卡类型": display_type,
                        "上次打卡时间": item.get("time") or "",
                        "打卡地点": address,
                    },
                }

        user_name = desensitize_name(config.get_value("userInfo.nikeName"))
        logger.info(f"用户 {user_name} 开始 {display_type} 打卡")

        # 打卡图片和备注
        img_count = config.get_value("config.clockIn.imageCount")
        if not isinstance(img_count, int) or img_count < 0:
            img_count = 1
            
        attachments = upload_img(
            api_client.get_upload_token(),
            config.get_value("userInfo.orgJson.snowFlakeId"),
            config.get_value("userInfo.userId"),
            img_count,
        )

        description_list = config.get_value("config.clockIn.description")
        description = random.choice(description_list) if description_list else None

        # 设置打卡信息
        checkin_info = {
            "type": checkin_type,
            "lastDetailAddress": last_checkin_info.get("address"),
            "attachments": attachments or None,
            "description": description,
            "createTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "attendenceTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "isReplace": 1 if replace else None,
        }

        if replace:
            api_client.submit_clock_in_replace(checkin_info)
        else:
            api_client.submit_clock_in(checkin_info)
        logger.info(f"用户 {user_name} {display_type} 打卡成功")

        return {
            "status": "success",
            "message": f"{display_type}打卡成功",
            "task_type": "打卡",
            "details": {
                "姓名": config.get_value("userInfo.nikeName"),
                "打卡类型": display_type,
                "打卡时间": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                "打卡地点": address,
                "补卡": "是" if replace else "否",
            },
        }
    except Exception as e:
        logger.error(f"打卡失败: {e}")
        err = str(e) or "unknown"
        tips = []
        if "clockIn.location.address" in err:
            tips.append("请先在用户配置中填写打卡地址，或使用“账号地址填充”")
        if "planId" in err:
            tips.append("请先获取并保存 planId（非教师账号需要）")
        if "token" in err or "Token" in err:
            tips.append("账号 token 失效时会自动重登；如持续失败请检查账号/密码")
        details = {
            "姓名": config.get_value("userInfo.nikeName"),
            "打卡时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "打卡地点": config.get_value("config.clockIn.location.address"),
        }
        if tips:
            details["建议"] = "；".join(tips)
        return {"status": "fail", "message": f"打卡失败: {err}", "task_type": "打卡", "details": details}


def perform_clock_in_makeup(
    api_client: ApiClient,
    config: ConfigManager,
    target_date: Optional[str],
    target_type: Optional[str] = None,
) -> Dict[str, Any]:
    day = parse_clockin_date(target_date)
    if not day:
        return {"status": "fail", "message": "补卡日期格式错误", "task_type": "补卡"}
    selected_type = str(target_type or "START").strip().upper()
    if selected_type and selected_type not in ("START", "END"):
        return {"status": "fail", "message": "补卡类型错误", "task_type": "补卡"}

    month_start, month_end = _month_bounds(datetime.combine(day, datetime_time()))
    records = api_client.get_checkin_records(month_start, month_end)
    existing_types = {item["type"] for item in normalize_clockin_records(records) if item["date"] == day.strftime("%Y-%m-%d")}
    missing_types = [item for item in ("START", "END") if item not in existing_types]
    target_types = [selected_type] if selected_type else missing_types
    target_types = [item for item in target_types if item in missing_types]
    if not target_types:
        label = CLOCKIN_TYPE_LABELS.get(selected_type, selected_type) if selected_type else "上班和下班"
        return {
            "status": "skip",
            "message": f"{day.strftime('%Y-%m-%d')} 已完成{label}打卡，无需补卡",
            "task_type": "补卡",
            "details": {
                "补卡日期": day.strftime("%Y-%m-%d"),
                "补卡类型": label,
            },
        }

    schedule = config.get_value("config.clockIn.schedule") or {}
    if not isinstance(schedule, dict):
        schedule = {}
    results: List[Dict[str, Any]] = []
    for checkin_type in target_types:
        hhmm = schedule.get("startTime") if checkin_type == "START" else schedule.get("endTime")
        default_hhmm = "07:30" if checkin_type == "START" else "18:00"
        target_time = combine_date_time(day, hhmm, default_hhmm)
        results.append(
            perform_clock_in(
                api_client,
                config,
                forced_checkin_type=checkin_type,
                target_time=target_time,
                replace=True,
            )
        )

    failed = [item for item in results if item.get("status") == "fail"]
    succeeded = [item for item in results if item.get("status") == "success"]
    labels = [CLOCKIN_TYPE_LABELS.get(item, item) for item in target_types]
    if failed:
        status = "fail"
        message = f"{day.strftime('%Y-%m-%d')} 补卡未全部完成"
    elif succeeded:
        status = "success"
        message = f"{day.strftime('%Y-%m-%d')} {'、'.join(labels)}补卡完成"
    else:
        status = "skip"
        message = f"{day.strftime('%Y-%m-%d')} 补卡已跳过"
    return {
        "status": status,
        "message": message,
        "task_type": "补卡",
        "details": {
            "补卡日期": day.strftime("%Y-%m-%d"),
            "补卡类型": "、".join(labels),
        },
        "items": results,
    }


def _normalize_makeup_dates(target_dates: Optional[List[Any]]) -> List[str]:
    dates: List[str] = []
    seen = set()
    for item in target_dates or []:
        day = parse_clockin_date(item)
        if not day:
            return []
        value = day.strftime("%Y-%m-%d")
        if value not in seen:
            seen.add(value)
            dates.append(value)
    return dates


def perform_clock_in_makeup_many(
    api_client: ApiClient,
    config: ConfigManager,
    target_dates: Optional[List[Any]],
    target_type: Optional[str] = None,
    delay_seconds: Optional[float] = None,
    rate_limit_retries: Optional[int] = None,
    rate_limit_retry_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    dates = _normalize_makeup_dates(target_dates)
    if not dates:
        return {"status": "fail", "message": "请选择有效的补卡日期", "task_type": "补卡"}

    delay = _clockin_makeup_batch_delay_seconds() if delay_seconds is None else max(float(delay_seconds), 0.0)
    retry_count = _clockin_makeup_rate_limit_retries() if rate_limit_retries is None else max(int(rate_limit_retries), 0)
    retry_seconds = _clockin_makeup_rate_limit_retry_seconds() if rate_limit_retry_seconds is None else max(float(rate_limit_retry_seconds), 0.0)
    results = []
    rate_limit_retry_total = 0
    for index, item in enumerate(dates):
        if index > 0 and delay > 0:
            time.sleep(delay)
        result, retries_used = _perform_clock_in_makeup_with_rate_limit_retry(
            api_client,
            config,
            item,
            target_type=target_type,
            retry_count=retry_count,
            retry_seconds=retry_seconds,
        )
        rate_limit_retry_total += retries_used
        results.append(result)
    failed = [item for item in results if item.get("status") == "fail"]
    succeeded = [item for item in results if item.get("status") == "success"]
    skipped = [item for item in results if item.get("status") == "skip"]
    if failed:
        status = "fail"
        message = f"{len(dates)} 天补卡未全部完成"
    elif succeeded:
        status = "success"
        message = f"{len(dates)} 天补卡完成"
    else:
        status = "skip"
        message = f"{len(dates)} 天补卡已跳过"
    return {
        "status": status,
        "message": message,
        "task_type": "补卡",
        "details": {
            "补卡天数": len(dates),
            "成功": len(succeeded),
            "失败": len(failed),
            "跳过": len(skipped),
            "请求间隔秒": delay,
            "频繁重试次数": rate_limit_retry_total,
            "频繁重试最大次数": retry_count,
            "频繁重试初始等待秒": retry_seconds,
        },
        "items": results,
    }


def _submit_report_common(
    api_client: ApiClient,
    config: ConfigManager,
    report_type: str,
    title_func: Callable[[int], str],
    check_time_func: Callable[[datetime], bool],
    get_submitted_func: Callable[[], Dict[str, Any]],
    paper_num_key: str,
    image_count_key: str,
    task_name: str,
    form_type: int,
    force_report: bool = False,
    target_period: Optional[str] = None,
) -> Dict[str, Any]:
    """通用日报/周报/月报提交逻辑"""

    # 映射 report_type 到 config key
    config_key_map = {"day": "daily", "week": "weekly", "month": "monthly"}
    config_key = config_key_map.get(report_type)

    if not force_report and not config.get_value(f"config.reportSettings.{config_key}.enabled"):
        logger.info(f"用户未开启{task_name}功能，跳过")
        now = datetime.now()
        return {
            "status": "skip",
            "message": f"用户未开启{task_name}功能",
            "task_type": task_name,
            "details": {
                "提交时间": now.strftime("%Y-%m-%d %H:%M:%S"),
                "开关": "未开启",
            },
        }

    target_time = _parse_report_target(report_type, target_period)
    current_time = target_time or datetime.now()

    # 检查提交时间
    if not force_report and not check_time_func(current_time):
        logger.info(f"未到{task_name}提交时间")
        return {
            "status": "skip",
            "message": f"未到{task_name}提交时间",
            "task_type": task_name,
            "details": {
                "提交时间": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                "开关": "已开启",
            },
        }

    try:
        # 检查是否已提交
        submitted_reports_info = get_submitted_func()
        submitted_reports = submitted_reports_info.get("data", [])

        count = submitted_reports_info.get("flag", 0) + 1
        title = title_func(count)
        weeks_label = f"第{count}周"

        if submitted_reports:
            should_skip = False
            for report in submitted_reports:
                if _same_report_period(report, report_type, current_time, weeks_label):
                    should_skip = True
                    break

            if should_skip:
                logger.info(f"本周期已经提交过{task_name}，跳过")
                return {
                    "status": "skip",
                    "message": f"本周期已经提交过{task_name}",
                    "task_type": task_name,
                }

        # 生成内容
        job_info = api_client.get_job_info()
        content = generate_article(
            config,
            title,
            job_info,
            config.get_value(paper_num_key),
            (submitted_reports or [])[:4]
        )

        # 上传图片
        img_count = config.get_value(image_count_key)
        if not isinstance(img_count, int) or img_count < 0:
            img_count = 1

        attachments = upload_img(
            api_client.get_upload_token(),
            config.get_value("userInfo.orgJson.snowFlakeId"),
            config.get_value("userInfo.userId"),
            img_count,
        )

        report_info = {
            "title": title,
            "content": content,
            "attachments": attachments,
            "reportType": report_type,
            "jobId": job_info.get("jobId", None),
            "reportTime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "formFieldDtoList": api_client.get_from_info(form_type),
        }

        # 特定类型的额外字段
        extra_details = {}
        if report_type == "week":
            start_date, end_date = _week_bounds(current_time)
            report_info["startTime"] = start_date
            report_info["endTime"] = end_date
            report_info["weeks"] = weeks_label
            extra_details = {
                "开始时间": report_info["startTime"],
                "结束时间": report_info["endTime"],
            }
        elif report_type == "month":
            report_info["yearmonth"] = current_time.strftime("%Y-%m")
            extra_details = {"提交月份": report_info["yearmonth"]}

        api_client.submit_report(report_info)

        logger.info(f"{title}已提交")

        return {
            "status": "success",
            "message": f"{title}已提交",
            "task_type": task_name,
            "details": {
                "标题": title,
                "提交时间": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                "附件": attachments,
                **extra_details,
            },
            "report_content": content,
        }

    except Exception as e:
        logger.error(f"{task_name}提交失败: {e}")
        return {
            "status": "fail",
            "message": f"{task_name}提交失败: {str(e)}",
            "task_type": task_name,
        }


def submit_daily_report(
    api_client: ApiClient,
    config: ConfigManager,
    force_report: bool = False,
    target_period: Optional[str] = None,
) -> Dict[str, Any]:
    """提交日报"""
    submit_days = config.get_value("config.reportSettings.daily.submitDays")
    submit_time = config.get_value("config.reportSettings.daily.submitTime")

    def parse_submit_time(value) -> tuple[int, int]:
        if isinstance(value, str) and ":" in value:
            try:
                hh, mm = value.split(":", 1)
                return int(hh), int(mm)
            except Exception:
                return 12, 0
        return 12, 0

    def check_time(t: datetime) -> bool:
        hh, mm = parse_submit_time(submit_time)
        if (t.hour < hh) or (t.hour == hh and t.minute < mm):
            return False
        if submit_days is None:
            return True
        if isinstance(submit_days, list):
            if len(submit_days) == 0:
                return False
            return (t.weekday() + 1) in submit_days
        return True

    return _submit_report_common(
        api_client=api_client,
        config=config,
        report_type="day",
        title_func=lambda c: f"第{c}天日报",
        check_time_func=check_time,
        get_submitted_func=lambda: api_client.get_submitted_reports_info("day"),
        paper_num_key="planInfo.planPaper.dayPaperNum",
        image_count_key="config.reportSettings.daily.imageCount",
        task_name="日报提交",
        form_type=7,
        force_report=force_report,
        target_period=target_period,
    )


def submit_weekly_report(
    config: ConfigManager,
    api_client: ApiClient,
    force_report: bool = False,
    target_period: Optional[str] = None,
) -> Dict[str, Any]:
    """提交周报"""
    submit_day = config.get_value("config.reportSettings.weekly.submitTime")
    submit_at = config.get_value("config.reportSettings.weekly.submitAt") or "12:00"

    def parse_submit_time(value) -> tuple[int, int]:
        if isinstance(value, str) and ":" in value:
            try:
                hh, mm = value.split(":", 1)
                return int(hh), int(mm)
            except Exception:
                return 12, 0
        return 12, 0

    def check_time(t: datetime) -> bool:
        hh, mm = parse_submit_time(submit_at)
        if not (t.weekday() + 1 == submit_day):
            return False
        return (t.hour > hh) or (t.hour == hh and t.minute >= mm)

    return _submit_report_common(
        api_client=api_client,
        config=config,
        report_type="week",
        title_func=lambda c: f"第{c}周周报",
        check_time_func=check_time,
        get_submitted_func=lambda: api_client.get_submitted_reports_info("week"),
        paper_num_key="planInfo.planPaper.weekPaperNum",
        image_count_key="config.reportSettings.weekly.imageCount",
        task_name="周报提交",
        form_type=8,
        force_report=force_report,
        target_period=target_period,
    )


def submit_monthly_report(
    config: ConfigManager,
    api_client: ApiClient,
    force_report: bool = False,
    target_period: Optional[str] = None,
) -> Dict[str, Any]:
    """提交月报"""
    submit_day = config.get_value("config.reportSettings.monthly.submitTime")
    submit_at = config.get_value("config.reportSettings.monthly.submitAt") or "12:00"
    # 默认每月20号
    if not isinstance(submit_day, int):
        submit_day = 20

    def parse_submit_time(value) -> tuple[int, int]:
        if isinstance(value, str) and ":" in value:
            try:
                hh, mm = value.split(":", 1)
                return int(hh), int(mm)
            except Exception:
                return 12, 0
        return 12, 0

    def check_time(t: datetime) -> bool:
        next_month = t.replace(day=28) + timedelta(days=4)
        last_day_of_month = (next_month - timedelta(days=next_month.day)).day
        target_day = min(submit_day, last_day_of_month)
        if t.day != target_day:
            return False
        hh, mm = parse_submit_time(submit_at)
        return (t.hour > hh) or (t.hour == hh and t.minute >= mm)

    return _submit_report_common(
        api_client=api_client,
        config=config,
        report_type="month",
        title_func=lambda c: f"第{c}月月报",
        check_time_func=check_time,
        get_submitted_func=lambda: api_client.get_submitted_reports_info("month"),
        paper_num_key="planInfo.planPaper.monthPaperNum",
        image_count_key="config.reportSettings.monthly.imageCount",
        task_name="月报提交",
        form_type=9,
        force_report=force_report,
        target_period=target_period,
    )


def run_task_by_config(
    config_data: Dict[str, Any],
    forced_checkin_type: Optional[str] = None,
    specific_task_type: Optional[str] = None,
    force_report: bool = False,
    target_period: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """根据配置字典执行任务"""
    config = ConfigManager(config=config_data)
    
    # 设置日志上下文
    try:
        nickname = desensitize_name(config.get_value("userInfo.nikeName")) or "?"
        _log_ctx.tag = f"{nickname}"
    except Exception:
        _log_ctx.tag = "-"

    results: List[Dict[str, Any]] = []
    pusher = None

    try:
        pusher = MessagePusher(
            build_effective_push_notifications(
                user_push=config.get_value("config.pushNotifications"),
                smtp_settings=_load_global_smtp_settings(),
            )
        )

        api_client = ApiClient(config)
        if not runtime_login_valid(config.get_value("userInfo")):
            api_client.login()

        logger.info("获取用户信息成功")

        if config.get_value("userInfo.userType") == "teacher":
            logger.info("用户身份为教师，跳过计划信息检查")
        elif not config.get_value("planInfo.planId"):
            api_client.fetch_internship_plan()
            logger.info("已获取实习计划信息")

        logger.info(
            f"开始执行：{desensitize_name(config.get_value('userInfo.nikeName'))}"
        )

        all_tasks = [
            ("clock_in", lambda: perform_clock_in(api_client, config, forced_checkin_type)),
            ("clock_in_makeup", lambda: perform_clock_in_makeup(api_client, config, target_period)),
            ("daily_report", lambda: submit_daily_report(api_client, config, force_report=force_report, target_period=target_period)),
            ("weekly_report", lambda: submit_weekly_report(config, api_client, force_report=force_report, target_period=target_period)),
            ("monthly_report", lambda: submit_monthly_report(config, api_client, force_report=force_report, target_period=target_period)),
        ]

        results = []
        for t_type, t_func in all_tasks:
            # 如果指定了任务类型，则只执行匹配的任务
            # report 匹配所有报告
            if specific_task_type:
                if specific_task_type == "report" and "report" in t_type:
                    pass
                elif specific_task_type != t_type:
                    continue
            
            results.append(t_func())

    except Exception as e:
        error_message = f"执行任务时发生严重错误: {str(e)}"
        logger.error(error_message)
        results.append(
            {"status": "fail", "message": error_message, "task_type": "系统错误"}
        )
    finally:
        if pusher:
            try:
                pusher.push(results)
            except Exception as e:
                logger.error(f"消息推送失败: {e}")

        logger.info(
            f"执行结束：{desensitize_name(config.get_value('userInfo.nikeName'))}"
        )
        _log_ctx.tag = "-"
        
    return results
