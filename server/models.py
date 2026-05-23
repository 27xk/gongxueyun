from typing import Optional, List, Dict, Any
from sqlmodel import Field, SQLModel, JSON, Column
from sqlalchemy import Index
from pydantic import BaseModel
import datetime

class UserBase(SQLModel):
    phone: str = Field(index=True, unique=True)
    password: str
    remark: Optional[str] = Field(default=None, index=True)
    app_password_hash: Optional[str] = Field(default=None)
    app_enabled: bool = Field(default=True, index=True)

    clockIn: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    reportSettings: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    ai: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    pushNotifications: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    device: str = "{brand: TA J20, systemVersion: 17, Platform: Android, isPhysicalDevice: true, incremental: K23V10A}"

    enable_clockin: bool = Field(default=True)

class User(UserBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    last_run_time: Optional[str] = None
    last_status: Optional[str] = None
    logs: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    last_execution_result: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    userInfo: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    planInfo: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

class UserCreate(UserBase):
    pass

class UserRead(UserBase):
    id: int
    last_run_time: Optional[str]
    last_status: Optional[str]
    logs: List[str]
    last_execution_result: List[Dict[str, Any]]

class UserListRead(SQLModel):
    id: int
    phone: str
    remark: Optional[str] = None
    enable_clockin: bool
    last_run_time: Optional[str]
    last_status: Optional[str]
    logs: List[str] = []

class UserUpdate(SQLModel):
    phone: Optional[str] = None
    password: Optional[str] = None
    remark: Optional[str] = None
    app_password_hash: Optional[str] = None
    app_enabled: Optional[bool] = None
    clockIn: Optional[Dict[str, Any]] = None
    reportSettings: Optional[Dict[str, Any]] = None
    ai: Optional[Dict[str, Any]] = None
    pushNotifications: Optional[List[Dict[str, Any]]] = None
    device: Optional[str] = None
    enable_clockin: Optional[bool] = None

class AuditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow, index=True)
    actor: str = Field(index=True)
    action: str = Field(index=True)
    target_user_id: Optional[int] = Field(default=None, index=True)
    detail: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

class BatchJob(SQLModel, table=True):
    __table_args__ = (
        Index("ix_batchjob_status_id", "status", "id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow, index=True)
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
    job_id: int = Field(index=True)
    user_id: int = Field(index=True)
    status: str = Field(default="queued", index=True)
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow, index=True)
    started_at: Optional[datetime.datetime] = Field(default=None, index=True)
    finished_at: Optional[datetime.datetime] = Field(default=None, index=True)
    error: Optional[str] = None
    attempts: int = Field(default=0, index=True)
    max_attempts: int = Field(default=3, index=True)
    next_run_at: Optional[datetime.datetime] = Field(default=None, index=True)

class AdminUser(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow, index=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: str = Field(default="admin", index=True)
    enabled: bool = Field(default=True, index=True)

class AppUser(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow, index=True)
    phone: str = Field(index=True, unique=True)
    password_hash: str
    enabled: bool = Field(default=True, index=True)
    bound_user_id: Optional[int] = Field(default=None, index=True)

class SystemSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
