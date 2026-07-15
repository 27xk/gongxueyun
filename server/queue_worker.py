import datetime
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select
from sqlalchemy import update, case, func

from server.batch_jobs import claim_batch_job_items, count_batch_job_items_by_status
from server.clockin_preauthorization import build_preauthorization_hooks
from server.database import engine
from server.models import DEFAULT_TENANT_ID, BatchJob, BatchJobItem, Tenant, User, AuditLog
from server.observability import record_task_event
from server.scheduler import user_to_config
from server.task_runner import run_task_by_config
from server.time_utils import utc_now
from server.user_runtime import apply_execution_results_to_user

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_executor: ThreadPoolExecutor | None = None
logger = logging.getLogger(__name__)
DEFAULT_RUNNING_ITEM_TIMEOUT_SECONDS = 30 * 60
MAX_RUNNING_ITEM_TIMEOUT_SECONDS = 24 * 60 * 60

def _now_utc() -> datetime.datetime:
    return utc_now()

def _running_item_timeout_seconds() -> int:
    raw = os.getenv("BATCH_RUNNING_ITEM_TIMEOUT_SECONDS") or str(DEFAULT_RUNNING_ITEM_TIMEOUT_SECONDS)
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_RUNNING_ITEM_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_RUNNING_ITEM_TIMEOUT_SECONDS
    return min(value, MAX_RUNNING_ITEM_TIMEOUT_SECONDS)

def _worker_owner() -> str:
    return os.getenv("BATCH_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"

def _same_tenant(left: str | None, right: str | None) -> bool:
    return str(left or DEFAULT_TENANT_ID) == str(right or DEFAULT_TENANT_ID)

def _tenant_is_active(session: Session, tenant_id: str | None) -> bool:
    normalized = str(tenant_id or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID
    tenant = session.get(Tenant, normalized)
    if tenant is None:
        return normalized == DEFAULT_TENANT_ID
    return str(tenant.status or "active").lower() == "active"

def _calc_backoff_seconds(attempts: int) -> int:
    base = int(os.getenv("BATCH_RETRY_BASE_SECONDS") or "3")
    cap = int(os.getenv("BATCH_RETRY_MAX_SECONDS") or "60")
    if attempts <= 0:
        return base
    seconds = base * (2 ** (attempts - 1))
    return min(cap, seconds)

def reclaim_stale_running_items(
    *,
    db_engine=None,
    now: datetime.datetime | None = None,
    timeout_seconds: int | None = None,
    limit: int = 200,
) -> int:
    db = db_engine or engine
    current = now or _now_utc()
    timeout = _running_item_timeout_seconds() if timeout_seconds is None else max(1, int(timeout_seconds))
    cutoff = current - datetime.timedelta(seconds=timeout)
    reclaimed = 0
    events: list[tuple[int, int, int, str, str]] = []

    with Session(db) as session:
        items = session.exec(
            select(BatchJobItem)
            .where(
                (BatchJobItem.status == "running")
                & (BatchJobItem.started_at.is_not(None))
                & (BatchJobItem.started_at < cutoff)
                & ((BatchJobItem.lease_until.is_(None)) | (BatchJobItem.lease_until < current))
            )
            .order_by(BatchJobItem.started_at.asc())
            .limit(max(1, int(limit)))
        ).all()
        for item in items:
            job = session.get(BatchJob, item.job_id)
            if not job:
                continue
            user_id = item.user_id
            error = f"Timed out after {timeout} seconds"
            if int(item.attempts or 0) < int(item.max_attempts or 3):
                item.status = "queued"
                item.error = error
                item.started_at = None
                item.finished_at = None
                item.next_run_at = current + datetime.timedelta(seconds=_calc_backoff_seconds(item.attempts))
                item.locked_by = None
                item.lock_token = None
                item.lease_until = None
                item.heartbeat_at = None
                event = "requeue_timeout"
                status = "queued"
            else:
                item.status = "fail"
                item.error = error
                item.finished_at = current
                item.locked_by = None
                item.lock_token = None
                item.lease_until = None
                item.heartbeat_at = None
                job.completed = int(job.completed or 0) + 1
                job.fail = int(job.fail or 0) + 1
                if int(job.completed or 0) >= int(job.total or 0):
                    job.status = "done"
                    job.finished_at = current
                else:
                    job.status = "running"
                session.add(job)
                event = "fail_timeout"
                status = "fail"
            session.add(item)
            reclaimed += 1
            events.append((int(job.id), int(item.id), int(user_id), event, status))
        session.commit()

    for job_id, item_id, user_id, event, status in events:
        record_task_event(
            source="queue_worker",
            event=event,
            task_key=f"batch:{job_id}:item:{item_id}",
            user_id=user_id,
            status=status,
            error=f"Timed out after {timeout} seconds",
            db_engine=db,
        )
    return reclaimed

def _touch_item_lease(
    item_id: int,
    lock_token: str | None,
    *,
    db_engine=None,
    now: datetime.datetime | None = None,
    timeout_seconds: int | None = None,
) -> bool:
    if not lock_token:
        return False
    db = db_engine or engine
    current = now or _now_utc()
    timeout = _running_item_timeout_seconds() if timeout_seconds is None else max(1, int(timeout_seconds))
    with Session(db) as session:
        item = session.get(BatchJobItem, item_id)
        if not item or item.status != "running" or item.lock_token != lock_token:
            return False
        item.heartbeat_at = current
        item.lease_until = current + datetime.timedelta(seconds=timeout)
        session.add(item)
        session.commit()
        return True

def _start_item_lease_heartbeat(item_id: int, lock_token: str | None) -> tuple[threading.Event, threading.Thread | None]:
    stop_event = threading.Event()
    if not lock_token:
        return stop_event, None

    timeout = _running_item_timeout_seconds()
    interval = max(1, min(60, timeout // 3 or 1))

    def _heartbeat_loop() -> None:
        while not stop_event.wait(interval):
            try:
                if not _touch_item_lease(item_id, lock_token, timeout_seconds=timeout):
                    return
            except Exception:
                logger.debug("queue item lease heartbeat failed", exc_info=True)

    thread = threading.Thread(target=_heartbeat_loop, name=f"batch-item-lease-{item_id}", daemon=True)
    thread.start()
    return stop_event, thread

def _finalize_item(job_id: int, item_id: int, ok: bool, error: str | None, lock_token: str | None = None) -> None:
    with Session(engine) as session:
        item = session.get(BatchJobItem, item_id)
        if not item:
            return
        if lock_token and item.lock_token != lock_token:
            return
        user_id = item.user_id
        item.status = "success" if ok else "fail"
        item.finished_at = _now_utc()
        item.error = error
        item.locked_by = None
        item.lock_token = None
        item.lease_until = None
        item.heartbeat_at = None
        session.add(item)
        now = _now_utc().isoformat(sep=" ", timespec="seconds")
        success_inc = 1 if ok else 0
        fail_inc = 0 if ok else 1
        session.exec(
            update(BatchJob)
            .where(BatchJob.id == job_id)
            .values(
                completed=BatchJob.completed + 1,
                success=BatchJob.success + success_inc,
                fail=BatchJob.fail + fail_inc,
                status=case((BatchJob.completed + 1 >= BatchJob.total, "done"), else_="running"),
                started_at=func.coalesce(BatchJob.started_at, now),
                finished_at=case((BatchJob.completed + 1 >= BatchJob.total, now), else_=BatchJob.finished_at),
            )
        )
        session.commit()
    record_task_event(
        source="queue_worker",
        event="finish",
        task_key=f"batch:{job_id}:item:{item_id}",
        user_id=user_id,
        status="success" if ok else "fail",
        error=error,
    )

def _run_item(job_id: int, item_id: int, lock_token: str | None = None) -> None:
    with Session(engine) as session:
        item = session.get(BatchJobItem, item_id)
        job = session.get(BatchJob, job_id)
        if not item or not job:
            return
        if lock_token and item.lock_token != lock_token:
            return
        if not _same_tenant(getattr(job, "tenant_id", None), getattr(item, "tenant_id", None)):
            _finalize_item(job_id, item_id, ok=False, error="Batch job tenant mismatch", lock_token=lock_token)
            return
        if not _tenant_is_active(session, getattr(job, "tenant_id", None)):
            _finalize_item(job_id, item_id, ok=False, error="Batch tenant disabled", lock_token=lock_token)
            return
        item.attempts = int(item.attempts or 0) + 1
        item.heartbeat_at = _now_utc()
        item.lease_until = _now_utc() + datetime.timedelta(seconds=_running_item_timeout_seconds())
        session.add(item)
        session.commit()
        user = session.get(User, item.user_id)
        if not user or getattr(user, "deleted_at", None) is not None:
            _finalize_item(job_id, item_id, ok=False, error="User not found", lock_token=lock_token)
            return
        if not _same_tenant(getattr(user, "tenant_id", None), getattr(item, "tenant_id", None)):
            _finalize_item(job_id, item_id, ok=False, error="Batch item tenant mismatch", lock_token=lock_token)
            return
        heartbeat_stop, heartbeat_thread = _start_item_lease_heartbeat(item_id, lock_token)
        try:
            config_data = user_to_config(user)
            results = run_task_by_config(
                config_data,
                preauthorization_hooks=build_preauthorization_hooks(
                    user,
                    db_engine=engine,
                ),
            )
            status = apply_execution_results_to_user(user, results, config_data)
            session.add(user)
            session.commit()
            if status == "Success":
                _finalize_item(job_id, item_id, ok=True, error=None, lock_token=lock_token)
            else:
                raise RuntimeError("Fail")
        except Exception as e:
            if item.attempts < int(item.max_attempts or 3):
                backoff = _calc_backoff_seconds(item.attempts)
                with Session(engine) as s2:
                    it2 = s2.get(BatchJobItem, item_id)
                    if it2 and it2.status == "running" and (not lock_token or it2.lock_token == lock_token):
                        it2.status = "queued"
                        it2.error = str(e)
                        it2.started_at = None
                        it2.finished_at = None
                        it2.next_run_at = _now_utc() + datetime.timedelta(seconds=backoff)
                        it2.locked_by = None
                        it2.lock_token = None
                        it2.lease_until = None
                        it2.heartbeat_at = None
                        s2.add(it2)
                        s2.commit()
                return
            _finalize_item(job_id, item_id, ok=False, error=str(e), lock_token=lock_token)
        finally:
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=1)

def _claim_items() -> None:
    global _executor
    if _executor is None:
        max_workers = int(os.getenv("BATCH_WORKER_MAX_CONCURRENCY") or "10")
        _executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, 50)))

    reclaim_stale_running_items()

    with Session(engine) as session:
        jobs = session.exec(
            select(BatchJob).where(BatchJob.status.in_(["queued", "running"])).order_by(BatchJob.id.asc()).limit(10)
        ).all()
        for job in jobs:
            if job.cancel_requested:
                session.exec(
                    update(BatchJobItem)
                    .where((BatchJobItem.job_id == job.id) & (BatchJobItem.status.in_(["queued"])))
                    .values(status="canceled", finished_at=_now_utc(), error="Canceled")
                )
                job.status = "canceled"
                job.finished_at = _now_utc()
                session.add(job)
                session.commit()
                continue

            if job.paused:
                if job.status != "paused":
                    job.status = "paused"
                    session.add(job)
                    session.commit()
                continue

            if job.total <= 0:
                job.status = "done"
                job.finished_at = _now_utc()
                session.add(job)
                session.commit()
                continue

            running_count = count_batch_job_items_by_status(session, job.id, "running")
            capacity = max(0, int(job.concurrency or 1) - running_count)
            if capacity <= 0:
                if job.status != "running":
                    job.status = "running"
                    job.started_at = job.started_at or _now_utc()
                    session.add(job)
                    session.commit()
                continue

            claimed_items = claim_batch_job_items(
                session,
                job.id,
                capacity,
                now=_now_utc(),
                owner=_worker_owner(),
                lease_seconds=_running_item_timeout_seconds(),
                return_claims=True,
            )
            if not claimed_items:
                if job.completed >= job.total and job.status != "done":
                    job.status = "done"
                    job.finished_at = _now_utc()
                    session.add(job)
                    session.commit()
                continue

            job.status = "running"
            job.started_at = job.started_at or _now_utc()
            session.add(job)
            session.commit()

            for claim in claimed_items:
                item_id = int(claim["item_id"])
                record_task_event(
                    source="queue_worker",
                    event="claim",
                    task_key=f"batch:{job.id}:item:{item_id}",
                    status="running",
                )
                _executor.submit(_run_item, job.id, item_id, claim.get("lock_token"))

def _loop() -> None:
    while not _stop_event.is_set():
        try:
            _claim_items()
        except Exception:
            logger.exception("queue worker claim loop failed")
        time.sleep(0.8)

def start_queue_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    with Session(engine) as session:
        session.add(AuditLog(actor="system", action="queue_worker.start", target_user_id=None, detail={}))
        session.commit()

def stop_queue_worker() -> None:
    global _executor
    _stop_event.set()
    if _executor:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


def is_queue_worker_running() -> bool:
    return bool(_thread and _thread.is_alive())
