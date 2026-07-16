import os
from pathlib import Path

from dotenv import load_dotenv
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy import inspect, text

from server.models import DEFAULT_TENANT_ID, Tenant

DATABASE_URL_ENV = "DATABASE_URL"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = PROJECT_ROOT / ".env"


def _load_project_dotenv() -> None:
    load_dotenv(DOTENV_PATH, override=False, encoding="utf-8-sig")


def _get_database_url() -> str:
    database_url = (os.getenv(DATABASE_URL_ENV) or "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required and must be a MySQL connection string")
    if not database_url.lower().startswith("mysql+pymysql://"):
        raise RuntimeError("Only MySQL is supported. DATABASE_URL must start with 'mysql+pymysql://'")
    return database_url


def _env_flag(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def should_run_runtime_schema_migrations() -> bool:
    configured = _env_flag("ALLOW_RUNTIME_SCHEMA_MIGRATIONS")
    if configured is not None:
        return configured
    app_env = (os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    return app_env not in {"prod", "production"}


def _alembic_config():
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "server" / "migrations"))
    return config


def upgrade_database_schema_to_head() -> None:
    from alembic import command

    command.upgrade(_alembic_config(), "head")


def _alembic_script_directory():
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config())


def get_alembic_heads() -> set[str]:
    return {str(head) for head in _alembic_script_directory().get_heads()}


def get_database_alembic_heads(db_engine=None) -> set[str]:
    from alembic.runtime.migration import MigrationContext

    db = db_engine if db_engine is not None else engine
    with db.connect() as connection:
        context = MigrationContext.configure(connection)
        return {str(head) for head in context.get_current_heads() if head is not None}


def require_database_schema_current(db_engine=None) -> None:
    expected_heads = get_alembic_heads()
    current_heads = get_database_alembic_heads(db_engine)
    if current_heads == expected_heads:
        return
    raise RuntimeError(
        "Database schema is not at Alembic head; "
        f"current={sorted(current_heads) or ['<none>']}, "
        f"expected={sorted(expected_heads) or ['<none>']}. "
        "Run `python -m alembic upgrade head` before startup."
    )


def _engine_options(database_url: str) -> dict:
    options = {"pool_pre_ping": True}
    if database_url.lower().startswith("mysql+pymysql://"):
        options.update(
            {
                "pool_size": _int_env("DATABASE_POOL_SIZE", 10, min_value=1, max_value=100),
                "max_overflow": _int_env("DATABASE_MAX_OVERFLOW", 20, min_value=0, max_value=200),
                "pool_recycle": _int_env("DATABASE_POOL_RECYCLE_SECONDS", 1800, min_value=60, max_value=24 * 3600),
                "pool_timeout": _int_env("DATABASE_POOL_TIMEOUT_SECONDS", 30, min_value=1, max_value=300),
            }
        )
    return options


_load_project_dotenv()

_DATABASE_URL = _get_database_url()
engine = create_engine(_DATABASE_URL, **_engine_options(_DATABASE_URL))


def ensure_user_runtime_columns(db_engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    if "user" not in table_names:
        return

    existing_columns = {column.get("name") for column in inspector.get_columns("user")}
    missing_columns = [
        column_name
        for column_name in ["userInfo", "planInfo", "deleted_at", "deleted_by", "delete_reason"]
        if column_name not in existing_columns
    ]
    if not missing_columns:
        _ensure_user_deleted_by_is_indexable(db_engine, inspector)
        return

    with db_engine.begin() as conn:
        for column_name in missing_columns:
            if column_name in {"userInfo", "planInfo"}:
                conn.execute(text(f"ALTER TABLE `user` ADD COLUMN `{column_name}` JSON NULL"))
            elif column_name == "deleted_at":
                conn.execute(text("ALTER TABLE `user` ADD COLUMN `deleted_at` DATETIME NULL"))
            elif column_name == "deleted_by":
                conn.execute(text("ALTER TABLE `user` ADD COLUMN `deleted_by` VARCHAR(255) NULL"))
            else:
                conn.execute(text(f"ALTER TABLE `user` ADD COLUMN `{column_name}` TEXT NULL"))

    _ensure_user_deleted_by_is_indexable(db_engine, inspector)


def _ensure_user_deleted_by_is_indexable(db_engine, inspector) -> None:
    if getattr(getattr(db_engine, "dialect", None), "name", "") != "mysql":
        return

    existing_columns = {column.get("name"): column for column in inspector.get_columns("user")}
    deleted_by = existing_columns.get("deleted_by")
    if not deleted_by:
        return

    column_type = deleted_by.get("type")
    column_type_name = column_type.__class__.__name__.lower() if column_type is not None else ""
    if not column_type_name.endswith("text"):
        return

    with db_engine.begin() as conn:
        conn.execute(text("ALTER TABLE `user` MODIFY COLUMN `deleted_by` VARCHAR(255) NULL"))


def ensure_runtime_tracking_columns(db_engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    column_specs = {
        "taskexecutionevent": [("request_id", "VARCHAR(128) NULL")],
        "httprequestmetric": [("request_id", "VARCHAR(128) NULL")],
    }
    with db_engine.begin() as conn:
        for table_name, columns in column_specs.items():
            if table_name not in table_names:
                continue
            existing_columns = {column.get("name") for column in inspector.get_columns(table_name)}
            for column_name, ddl in columns:
                if column_name in existing_columns:
                    continue
                conn.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {ddl}"))


def ensure_auth_token_version_columns(db_engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    column_specs = {
        "adminuser": {
            "token_version": "INTEGER NOT NULL DEFAULT 0",
            "failed_login_count": "INTEGER NOT NULL DEFAULT 0",
            "locked_until": "DATETIME NULL",
        },
        "appuser": {
            "token_version": "INTEGER NOT NULL DEFAULT 0",
            "failed_login_count": "INTEGER NOT NULL DEFAULT 0",
            "locked_until": "DATETIME NULL",
        },
    }
    with db_engine.begin() as conn:
        for table_name, specs in column_specs.items():
            if table_name not in table_names:
                continue
            existing_columns = {column.get("name") for column in inspector.get_columns(table_name)}
            for column_name, ddl in specs.items():
                if column_name in existing_columns:
                    continue
                conn.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {ddl}"))


def ensure_batch_job_runtime_columns(db_engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    column_specs = {
        "batchjob": {
            "cancel_requested": "BOOLEAN NOT NULL DEFAULT 0",
            "paused": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "batchjobitem": {
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "next_run_at": "DATETIME NULL",
            "locked_by": "VARCHAR(255) NULL",
            "lock_token": "VARCHAR(255) NULL",
            "lease_until": "DATETIME NULL",
            "heartbeat_at": "DATETIME NULL",
        },
    }
    with db_engine.begin() as conn:
        for table_name, specs in column_specs.items():
            if table_name not in table_names:
                continue
            existing_columns = {column.get("name") for column in inspector.get_columns(table_name)}
            for column_name, ddl in specs.items():
                if column_name in existing_columns:
                    continue
                conn.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {ddl}"))


def ensure_legacy_admin_mfa_removed(db_engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    if "adminuser" not in table_names:
        return

    legacy_columns = ("mfa_enabled", "mfa_totp_secret", "mfa_confirmed_at")
    legacy_indexes = ("ix_adminuser_mfa_enabled", "ix_adminuser_mfa_confirmed_at")
    existing_columns = {column.get("name") for column in inspector.get_columns("adminuser")}
    existing_indexes = {str(index.get("name") or "") for index in inspector.get_indexes("adminuser")}
    columns_to_drop = [column for column in legacy_columns if column in existing_columns]
    indexes_to_drop = [index for index in legacy_indexes if index in existing_indexes]
    if not columns_to_drop and not indexes_to_drop:
        return

    dialect_name = getattr(getattr(db_engine, "dialect", None), "name", "")
    with db_engine.begin() as conn:
        for index_name in indexes_to_drop:
            if dialect_name == "mysql":
                conn.execute(text(f"DROP INDEX `{index_name}` ON `adminuser`"))
            else:
                conn.execute(text(f"DROP INDEX `{index_name}`"))
        for column_name in columns_to_drop:
            conn.execute(text(f"ALTER TABLE `adminuser` DROP COLUMN `{column_name}`"))


def ensure_tenant_columns(db_engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    scoped_tables = [
        "user",
        "auditlog",
        "batchjob",
        "batchjobitem",
        "adminuser",
        "appuser",
        "systemsetting",
        "taskexecutionlock",
        "taskexecutionevent",
        "httprequestmetric",
    ]
    with db_engine.begin() as conn:
        for table_name in scoped_tables:
            if table_name not in table_names:
                continue
            existing_columns = {column.get("name") for column in inspector.get_columns(table_name)}
            if "tenant_id" in existing_columns:
                continue
            conn.execute(
                text(
                    f"ALTER TABLE `{table_name}` "
                    "ADD COLUMN `tenant_id` VARCHAR(255) NOT NULL DEFAULT 'default'"
                )
            )


def ensure_default_tenant(db_engine) -> None:
    with Session(db_engine) as session:
        tenant = session.get(Tenant, DEFAULT_TENANT_ID)
        if tenant is not None:
            return
        session.add(Tenant(id=DEFAULT_TENANT_ID, name="Default Tenant", status="active"))
        session.commit()


def ensure_runtime_indexes(db_engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    index_specs = {
        "user": {
            "ix_user_tenant_id": ["tenant_id"],
            "ix_user_deleted_at": ["deleted_at"],
            "ix_user_deleted_by": ["deleted_by"],
        },
        "auditlog": {
            "ix_auditlog_tenant_id": ["tenant_id"],
        },
        "batchjobitem": {
            "ix_batchjobitem_tenant_id": ["tenant_id"],
            "ix_batchjobitem_job_status_id": ["job_id", "status", "id"],
            "ix_batchjobitem_job_status_next_run_id": ["job_id", "status", "next_run_at", "id"],
            "ix_batchjobitem_locked_by": ["locked_by"],
            "ix_batchjobitem_lock_token": ["lock_token"],
            "ix_batchjobitem_lease_until": ["lease_until"],
            "ix_batchjobitem_heartbeat_at": ["heartbeat_at"],
        },
        "adminuser": {
            "ix_adminuser_tenant_id": ["tenant_id"],
            "ix_adminuser_token_version": ["token_version"],
            "ix_adminuser_failed_login_count": ["failed_login_count"],
            "ix_adminuser_locked_until": ["locked_until"],
        },
        "appuser": {
            "ix_appuser_tenant_id": ["tenant_id"],
            "ix_appuser_token_version": ["token_version"],
            "ix_appuser_failed_login_count": ["failed_login_count"],
            "ix_appuser_locked_until": ["locked_until"],
        },
        "systemsetting": {
            "ix_systemsetting_tenant_id": ["tenant_id"],
        },
        "batchjob": {
            "ix_batchjob_tenant_id": ["tenant_id"],
            "ix_batchjob_status_id": ["status", "id"],
        },
        "ratelimitevent": {
            "ix_ratelimitevent_bucket_created": ["bucket_key", "created_at"],
        },
        "ratelimitbucket": {
            "ix_ratelimitbucket_bucket_key": ["bucket_key"],
            "ix_ratelimitbucket_updated": ["updated_at"],
        },
        "taskexecutionlock": {
            "ix_taskexecutionlock_tenant_id": ["tenant_id"],
            "ix_taskexecutionlock_expires_at": ["expires_at"],
        },
        "taskexecutionevent": {
            "ix_taskexecutionevent_tenant_id": ["tenant_id"],
            "ix_taskexecutionevent_source_created": ["source", "created_at"],
            "ix_taskexecutionevent_status_created": ["status", "created_at"],
            "ix_taskexecutionevent_user_created": ["user_id", "created_at"],
            "ix_taskexecutionevent_request_created": ["request_id", "created_at"],
        },
        "httprequestmetric": {
            "ix_httprequestmetric_tenant_id": ["tenant_id"],
            "ix_httprequestmetric_status_created": ["status_code", "created_at"],
            "ix_httprequestmetric_path_created": ["path", "created_at"],
            "ix_httprequestmetric_request_created": ["request_id", "created_at"],
        },
    }

    with db_engine.begin() as conn:
        for table_name, specs in index_specs.items():
            if table_name not in table_names:
                continue
            existing_columns = {column.get("name") for column in inspector.get_columns(table_name)}
            existing_indexes = {
                str(item.get("name") or "").lower()
                for item in inspector.get_indexes(table_name)
            }
            for index_name, columns in specs.items():
                if index_name.lower() in existing_indexes:
                    continue
                if any(column not in existing_columns for column in columns):
                    continue
                columns_sql = ", ".join(f"`{column}`" for column in columns)
                conn.execute(text(f"CREATE INDEX `{index_name}` ON `{table_name}` ({columns_sql})"))


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    ensure_tenant_columns(engine)
    ensure_user_runtime_columns(engine)
    ensure_runtime_tracking_columns(engine)
    ensure_auth_token_version_columns(engine)
    ensure_batch_job_runtime_columns(engine)
    ensure_legacy_admin_mfa_removed(engine)
    ensure_runtime_indexes(engine)
    ensure_default_tenant(engine)


def get_session():
    with Session(engine) as session:
        yield session
