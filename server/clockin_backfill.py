import datetime
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence


CLOCKIN_TYPE_LABELS = {
    "START": "上班",
    "END": "下班",
    "HOLIDAY": "休息",
}
DEFAULT_REQUIRED_TYPES = ("START", "END")


def parse_clockin_date(value: Any) -> Optional[datetime.date]:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value

    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("/", "-")
    if raw.isdigit() and len(raw) >= 13:
        try:
            return datetime.datetime.fromtimestamp(int(raw[:13]) / 1000).date()
        except Exception:
            return None

    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10), ("%Y%m%d", 8)):
        try:
            return datetime.datetime.strptime(raw[:size], fmt).date()
        except Exception:
            continue

    match = re.search(r"20\d{2}-\d{1,2}-\d{1,2}", raw)
    if match:
        try:
            return datetime.datetime.strptime(match.group(0), "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def parse_hhmm(value: Any, default: str) -> tuple[int, int]:
    raw = str(value or default).strip()
    if ":" not in raw:
        raw = default
    try:
        hh, mm = raw.split(":", 1)
        return max(0, min(int(hh), 23)), max(0, min(int(mm), 59))
    except Exception:
        hh, mm = default.split(":", 1)
        return int(hh), int(mm)


def combine_date_time(day: datetime.date, hhmm: Any, default: str) -> datetime.datetime:
    hh, mm = parse_hhmm(hhmm, default)
    return datetime.datetime.combine(day, datetime.time(hour=hh, minute=mm))


def _record_datetime(record: Dict[str, Any]) -> Optional[datetime.datetime]:
    for key in ("createTime", "attendenceTime", "attendanceTime", "modifiedTime", "time"):
        raw = str(record.get(key) or "").strip()
        if not raw:
            continue
        raw = raw.replace("/", "-")
        for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
            try:
                parsed = datetime.datetime.strptime(raw[:size], fmt)
                if size == 10:
                    return datetime.datetime.combine(parsed.date(), datetime.time())
                return parsed
            except Exception:
                continue
    day = parse_clockin_date(record.get("date"))
    if day:
        return datetime.datetime.combine(day, datetime.time())
    return None


def normalize_clockin_records(records: Iterable[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        record_type = str(record.get("type") or "").strip().upper()
        if not record_type:
            continue
        record_time = _record_datetime(record)
        if not record_time:
            continue
        normalized.append(
            {
                "date": record_time.strftime("%Y-%m-%d"),
                "time": record_time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": record_type,
                "type_label": CLOCKIN_TYPE_LABELS.get(record_type, record_type),
                "address": str(
                    record.get("address")
                    or record.get("detailAddress")
                    or record.get("lastDetailAddress")
                    or record.get("practiceAddress")
                    or ""
                ).strip(),
            }
        )
    normalized.sort(key=lambda item: (item["date"], item["time"], item["type"]), reverse=True)
    return normalized


def _coerce_date(value: Any) -> datetime.date:
    parsed = parse_clockin_date(value)
    if not parsed:
        raise ValueError("日期格式错误")
    return parsed


def _scheduled_weekdays(weekdays: Sequence[Any] | None) -> Optional[set[int]]:
    if weekdays is None:
        return None
    out: set[int] = set()
    for item in weekdays:
        try:
            day = int(item)
        except Exception:
            continue
        if 1 <= day <= 7:
            out.add(day)
    return out


def build_missing_clockin_day_options(
    records: Iterable[Dict[str, Any]] | None,
    start_date: Any,
    end_date: Any,
    *,
    scheduled_weekdays: Sequence[Any] | None = None,
    respect_scheduled_weekdays: bool = True,
    required_types: Sequence[str] = DEFAULT_REQUIRED_TYPES,
) -> List[Dict[str, Any]]:
    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    if start > end:
        start = end

    weekdays = _scheduled_weekdays(scheduled_weekdays) if respect_scheduled_weekdays else None
    required = [str(item or "").strip().upper() for item in required_types if str(item or "").strip()]
    by_date: Dict[str, set[str]] = {}
    for item in normalize_clockin_records(records):
        by_date.setdefault(item["date"], set()).add(item["type"])

    options: List[Dict[str, Any]] = []
    day = end
    while day >= start:
        weekday = day.weekday() + 1
        if weekdays is None or weekday in weekdays:
            value = day.strftime("%Y-%m-%d")
            existing_types = [item for item in required if item in by_date.get(value, set())]
            missing_types = [item for item in required if item not in by_date.get(value, set())]
            if missing_types:
                missing_labels = [CLOCKIN_TYPE_LABELS.get(item, item) for item in missing_types]
                today = "（今天）" if day == datetime.date.today() else ""
                options.append(
                    {
                        "value": value,
                        "label": f"{value}{today}（缺{'、'.join(missing_labels)}）",
                        "missing_types": missing_types,
                        "existing_types": existing_types,
                    }
                )
        day -= datetime.timedelta(days=1)
    return options
