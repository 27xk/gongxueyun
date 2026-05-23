import datetime
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select
from sqlalchemy import update, case, func

from server.batch_jobs import count_batch_job_items_by_status
from server.database import engine
from server.models import BatchJob, BatchJobItem, User, AuditLog
from server.scheduler import user_to_config
from server.task_runner import run_task_by_config
from server.user_runtime import apply_execution_results_to_user

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_executor: ThreadPoolExecutor | None = None

def _now_utc() -> datetime.datetime:
    return datetime.datetime.utcnow()

def _calc_backoff_seconds(attempts: int) -> int:
    base = int(os.getenv("BATCH_RETRY_BASE_SECONDS") or "3")
    cap = int(os.getenv("BATCH_RETRY_MAX_SECONDS") or "60")
    if attempts <= 0:
        return base
    seconds = base * (2 ** (attempts - 1))
    return min(cap, seconds)

def _finalize_item(job_id: int, item_id: int, ok: bool, error: str | None) -> None:
    with Session(engine) as session:
        item = session.get(BatchJobItem, item_id)
        if not item:
            return
        item.status = "success" if ok else "fail"
        item.finished_at = datetime.datetime.utcnow()
        item.error = error
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

def _run_item(job_id: int, item_id: int) -> None:
    with Session(engine) as session:
        item = session.get(BatchJobItem, item_id)
        job = session.get(BatchJob, job_id)
        if not item or not job:
            return
        item.attempts = int(item.attempts or 0) + 1
        session.add(item)
        session.commit()
        user = session.get(User, item.user_id)
        if not user:
            _finalize_item(job_id, item_id, ok=False, error="User not found")
            return
        config_data = user_to_config(user)
        try:
            results = run_task_by_config(config_data)
            status = apply_execution_results_to_user(user, results, config_data)
            session.add(user)
            session.commit()
            if status == "Success":
                _finalize_item(job_id, item_id, ok=True, error=None)
            else:
                raise RuntimeError("Fail")
        except Exception as e:
            if item.attempts < int(item.max_attempts or 3):
                backoff = _calc_backoff_seconds(item.attempts)
                with Session(engine) as s2:
                    it2 = s2.get(BatchJobItem, item_id)
                    if it2 and it2.status == "running":
                        it2.status = "queued"
                        it2.error = str(e)
                        it2.started_at = None
                        it2.finished_at = None
                        it2.next_run_at = _now_utc() + datetime.timedelta(seconds=backoff)
                        s2.add(it2)
                        s2.commit()
                return
            _finalize_item(job_id, item_id, ok=False, error=str(e))

def _claim_items() -> None:
    global _executor
    if _executor is None:
        max_workers = int(os.getenv("BATCH_WORKER_MAX_CONCURRENCY") or "10")
        _executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, 50)))

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
                job.finished_at = datetime.datetime.utcnow()
                session.add(job)
                session.commit()
                continue

            running_count = count_batch_job_items_by_status(session, job.id, "running")
            capacity = max(0, int(job.concurrency or 1) - running_count)
            if capacity <= 0:
                if job.status != "running":
                    job.status = "running"
                    job.started_at = job.started_at or datetime.datetime.utcnow()
                    session.add(job)
                    session.commit()
                continue

            queued_items = session.exec(
                select(BatchJobItem)
                .where(
                    (BatchJobItem.job_id == job.id)
                    & (BatchJobItem.status == "queued")
                    & ((BatchJobItem.next_run_at.is_(None)) | (BatchJobItem.next_run_at <= _now_utc()))
                )
                .order_by(BatchJobItem.id.asc())
                .limit(capacity)
            ).all()
            if not queued_items:
                if job.completed >= job.total and job.status != "done":
                    job.status = "done"
                    job.finished_at = datetime.datetime.utcnow()
                    session.add(job)
                    session.commit()
                continue

            job.status = "running"
            job.started_at = job.started_at or datetime.datetime.utcnow()
            session.add(job)
            session.commit()

            for item in queued_items:
                item.status = "running"
                item.started_at = datetime.datetime.utcnow()
                session.add(item)
            session.commit()

            for item in queued_items:
                _executor.submit(_run_item, job.id, item.id)

def _loop() -> None:
    while not _stop_event.is_set():
        try:
            _claim_items()
        except Exception:
            pass
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
