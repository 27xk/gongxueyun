import os
import threading
import time
from collections import OrderedDict

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from server.security import is_production

_MEMORY_LOCK = threading.Lock()
_MEMORY_BUCKETS: OrderedDict[str, list[float]] = OrderedDict()
_MAX_MEMORY_BUCKETS = 4096
_LAST_DB_BUCKET_PURGE_AT = 0.0
_DB_BUCKET_PURGE_INTERVAL_SECONDS = 60.0


def _backend() -> str:
    configured = (os.getenv("RATE_LIMIT_BACKEND") or "").strip().lower()
    if configured:
        return configured
    return "database" if is_production() else "memory"


def _check_memory(key: str, limit: int, per_seconds: int) -> None:
    now = time.time()
    cutoff = now - per_seconds
    with _MEMORY_LOCK:
        bucket = [t for t in _MEMORY_BUCKETS.get(key, []) if t >= cutoff]
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
        bucket.append(now)
        _MEMORY_BUCKETS[key] = bucket
        _MEMORY_BUCKETS.move_to_end(key)
        while len(_MEMORY_BUCKETS) > _MAX_MEMORY_BUCKETS:
            _MEMORY_BUCKETS.popitem(last=False)


def _check_database(key: str, limit: int, per_seconds: int) -> None:
    from server.database import engine
    from server.models import RateLimitBucket

    global _LAST_DB_BUCKET_PURGE_AT
    now = time.time()
    cutoff = now - per_seconds
    with Session(engine) as session:
        if now - _LAST_DB_BUCKET_PURGE_AT >= _DB_BUCKET_PURGE_INTERVAL_SECONDS:
            session.exec(delete(RateLimitBucket).where(RateLimitBucket.updated_at < now - max(3600, per_seconds * 2)))
            _LAST_DB_BUCKET_PURGE_AT = now

        bucket = session.exec(
            select(RateLimitBucket)
            .where(RateLimitBucket.bucket_key == key)
            .with_for_update()
        ).first()
        if bucket is None:
            bucket = RateLimitBucket(bucket_key=key, window_start=now, count=0, updated_at=now)
            session.add(bucket)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                bucket = session.exec(select(RateLimitBucket).where(RateLimitBucket.bucket_key == key)).first()
                if bucket is None:
                    raise
        elif float(bucket.window_start or 0) < cutoff:
            bucket.window_start = now
            bucket.count = 0

        if int(bucket.count or 0) >= limit:
            bucket.updated_at = now
            session.commit()
            raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
        bucket.count = int(bucket.count or 0) + 1
        bucket.updated_at = now
        session.add(bucket)
        session.commit()


def check_rate_limit(key: str, limit: int, per_seconds: int, detail: str | None = None) -> None:
    try:
        if _backend() == "database":
            _check_database(key, limit, per_seconds)
        else:
            _check_memory(key, limit, per_seconds)
    except HTTPException as exc:
        if detail:
            exc.detail = detail
        raise


def clear_memory_rate_limits() -> None:
    with _MEMORY_LOCK:
        _MEMORY_BUCKETS.clear()
