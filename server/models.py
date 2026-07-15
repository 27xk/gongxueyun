from typing import Optional, List, Dict, Any
from sqlmodel import Field, SQLModel, JSON, Column
from sqlalchemy import Index, UniqueConstraint
import datetime

from server.time_utils import utc_now

DEFAULT_TENANT_ID = "default"


class Tenant(SQLModel, table=True):
    id: str = Field(default=DEFAULT_TENANT_ID, primary_key=True)
    name: str = Field(default="Default Tenant", index=True)
    status: str = Field(default="active", index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    settings: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class UserBase(SQLModel):
    phone: str = Field(index=True)
    password: str
    remark: Optional[str] = Field(default=None, index=True)
    app_password_hash: Optional[str] = Field(default=None)
    app_enabled: bool = Field(default=True, index=True)

    clockIn: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    reportSettings: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    pushNotifications: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    device: str = "{brand: TA J20, systemVersion: 17, Platform: Android, isPhysicalDevice: true, incremental: K23V10A}"

    enable_clockin: bool = Field(default=True)

class User(UserBase, table=True):
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone", name="uq_user_tenant_phone"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    deleted_at: Optional[datetime.datetime] = Field(default=None, index=True)
    deleted_by: Optional[str] = Field(default=None, index=True)
    delete_reason: Optional[str] = None
    last_run_time: Optional[str] = None
    last_status: Optional[str] = None
    logs: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    last_execution_result: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    ai: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    userInfo: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    planInfo: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ClockInPreauthorization(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "target_date",
            "target_type",
            name="uq_clockinpreauthorization_target",
        ),
        Index(
            "ix_clockinpreauthorization_user_date",
            "tenant_id",
            "user_id",
            "target_date",
        ),
        Index(
            "ix_clockinpreauthorization_user_status_date",
            "tenant_id",
            "user_id",
            "status",
            "target_date",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID)
    user_id: int
    target_date: datetime.date
    target_type: str
    status: str = "authorized"
    out_register_no: str
    authorized_at: datetime.datetime
    consumed_at: Optional[datetime.datetime] = None
    used_target_type: Optional[str] = None
    created_at: datetime.datetime = Field(default_factory=utc_now)
    updated_at: datetime.datetime = Field(default_factory=utc_now)

class UserCreate(UserBase):
    pass

class UserRead(UserBase):
    id: int
    deleted_at: Optional[datetime.datetime] = None
    last_run_time: Optional[str]
    last_status: Optional[str]
    logs: List[str]
    last_execution_result: List[Dict[str, Any]]

class UserListRead(SQLModel):
    id: int
    phone: str
    remark: Optional[str] = None
    deleted_at: Optional[datetime.datetime] = None
    enable_clockin: bool
    last_run_time: Optional[str]
    last_status: Optional[str]
    logs: List[str] = Field(default_factory=list)

class UserUpdate(SQLModel):
    phone: Optional[str] = None
    password: Optional[str] = None
    remark: Optional[str] = None
    app_password_hash: Optional[str] = None
    app_enabled: Optional[bool] = None
    clockIn: Optional[Dict[str, Any]] = None
    reportSettings: Optional[Dict[str, Any]] = None
    pushNotifications: Optional[List[Dict[str, Any]]] = None
    device: Optional[str] = None
    enable_clockin: Optional[bool] = None

class AuditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    actor: str = Field(index=True)
    action: str = Field(index=True)
    target_user_id: Optional[int] = Field(default=None, index=True)
    detail: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

class BatchJob(SQLModel, table=True):
    __table_args__ = (
        Index("ix_batchjob_status_id", "status", "id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    created_by: str = Field(index=True)
    status: str = Field(default="queued", index=True)
    started_at: Optional[datetime.datetime] = Field(default=None, index=True)
    finished_at: Optional[datetime.datetime] = Field(default=None, index=True)
    total: int = 0
    completed: int = 0
    success: int = 0
    fail: int = 0
    concurrency: int = 1
    user_ids: List[int] = Field(default_factory=list, sa_column=Column(JSON))
    last_errors: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    cancel_requested: bool = Field(default=False, index=True)
    paused: bool = Field(default=False, index=True)

class BatchJobItem(SQLModel, table=True):
    __table_args__ = (
        Index("ix_batchjobitem_job_status_id", "job_id", "status", "id"),
        Index("ix_batchjobitem_job_status_next_run_id", "job_id", "status", "next_run_at", "id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    job_id: int = Field(index=True)
    user_id: int = Field(index=True)
    status: str = Field(default="queued", index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    started_at: Optional[datetime.datetime] = Field(default=None, index=True)
    finished_at: Optional[datetime.datetime] = Field(default=None, index=True)
    error: Optional[str] = None
    attempts: int = Field(default=0, index=True)
    max_attempts: int = Field(default=3, index=True)
    next_run_at: Optional[datetime.datetime] = Field(default=None, index=True)
    locked_by: Optional[str] = Field(default=None, index=True)
    lock_token: Optional[str] = Field(default=None, index=True)
    lease_until: Optional[datetime.datetime] = Field(default=None, index=True)
    heartbeat_at: Optional[datetime.datetime] = Field(default=None, index=True)

class AdminUser(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_adminuser_tenant_username"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    username: str = Field(index=True)
    password_hash: str
    role: str = Field(default="admin", index=True)
    enabled: bool = Field(default=True, index=True)
    token_version: int = Field(default=0, index=True)
    failed_login_count: int = Field(default=0, index=True)
    locked_until: Optional[datetime.datetime] = Field(default=None, index=True)

class AppUser(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone", name="uq_appuser_tenant_phone"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    phone: str = Field(index=True)
    password_hash: str
    enabled: bool = Field(default=True, index=True)
    bound_user_id: Optional[int] = Field(default=None, index=True)
    token_version: int = Field(default=0, index=True)
    failed_login_count: int = Field(default=0, index=True)
    locked_until: Optional[datetime.datetime] = Field(default=None, index=True)

class SystemSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    value: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class RateLimitEvent(SQLModel, table=True):
    __table_args__ = (
        Index("ix_ratelimitevent_bucket_created", "bucket_key", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    bucket_key: str = Field(index=True)
    created_at: float = Field(index=True)


class RateLimitBucket(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("bucket_key", name="uq_ratelimitbucket_bucket_key"),
        Index("ix_ratelimitbucket_updated", "updated_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    bucket_key: str = Field(index=True)
    window_start: float = Field(index=True)
    count: int = Field(default=0, index=True)
    updated_at: float = Field(index=True)


class TaskExecutionLock(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("lock_key", name="uq_taskexecutionlock_lock_key"),
        Index("ix_taskexecutionlock_expires_at", "expires_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    lock_key: str
    owner: str = Field(index=True)
    acquired_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    expires_at: datetime.datetime
    detail: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class TaskExecutionEvent(SQLModel, table=True):
    __table_args__ = (
        Index("ix_taskexecutionevent_source_created", "source", "created_at"),
        Index("ix_taskexecutionevent_status_created", "status", "created_at"),
        Index("ix_taskexecutionevent_user_created", "user_id", "created_at"),
        Index("ix_taskexecutionevent_request_created", "request_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    source: str
    event: str = Field(index=True)
    task_key: str = Field(index=True)
    user_id: Optional[int] = Field(default=None)
    status: Optional[str] = Field(default=None)
    request_id: Optional[str] = Field(default=None, index=True)
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class HttpRequestMetric(SQLModel, table=True):
    __table_args__ = (
        Index("ix_httprequestmetric_status_created", "status_code", "created_at"),
        Index("ix_httprequestmetric_path_created", "path", "created_at"),
        Index("ix_httprequestmetric_request_created", "request_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(default=DEFAULT_TENANT_ID, index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now, index=True)
    method: str = Field(index=True)
    path: str = Field(index=True)
    status_code: int = Field(index=True)
    request_id: Optional[str] = Field(default=None, index=True)
    duration_ms: int = Field(default=0, index=True)
