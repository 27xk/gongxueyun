from typing import Iterable

from sqlalchemy import func
from sqlmodel import Session, select

from server.models import BatchJobItem


def get_batch_job_item_status_counts(
    session: Session,
    job_id: int,
    statuses: Iterable[str] | None = None,
) -> dict[str, int]:
    stmt = select(BatchJobItem.status, func.count()).where(BatchJobItem.job_id == job_id)
    if statuses is not None:
        status_list = [str(item) for item in statuses]
        if not status_list:
            return {}
        stmt = stmt.where(BatchJobItem.status.in_(status_list))
    rows = session.exec(stmt.group_by(BatchJobItem.status)).all()
    return {str(status): int(count or 0) for status, count in rows}


def count_batch_job_items_by_status(session: Session, job_id: int, status: str) -> int:
    return get_batch_job_item_status_counts(session, job_id, [status]).get(status, 0)
