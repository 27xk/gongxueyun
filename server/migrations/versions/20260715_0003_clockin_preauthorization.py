"""Add clock-in preauthorization storage.

Revision ID: 20260715_0003
Revises: 20260530_0002
Create Date: 2026-07-15
"""

import datetime
import json

from alembic import op
import sqlalchemy as sa


revision = "20260715_0003"
down_revision = "20260530_0002"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _existing_columns(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _existing_indexes(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _schedule_start_utc(clock_in: object) -> datetime.datetime | None:
    value = clock_in
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if not isinstance(value, dict):
        return None
    schedule = value.get("schedule")
    if not isinstance(schedule, dict):
        return None
    try:
        target = datetime.datetime.strptime(
            str(schedule.get("startDate") or "")[:10],
            "%Y-%m-%d",
        )
    except Exception:
        return None
    return target - datetime.timedelta(hours=8)


def _backfill_user_created_at() -> None:
    bind = op.get_bind()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    rows = bind.execute(
        sa.text("SELECT id, clockIn FROM user WHERE created_at IS NULL")
    ).mappings()
    has_audit_log = _table_exists("auditlog")
    for row in rows:
        created_at = None
        if has_audit_log:
            created_at = bind.execute(
                sa.text(
                    "SELECT MIN(created_at) FROM auditlog "
                    "WHERE action = 'user.create' AND target_user_id = :user_id"
                ),
                {"user_id": row["id"]},
            ).scalar()
        created_at = created_at or _schedule_start_utc(row.get("clockIn")) or now
        bind.execute(
            sa.text("UPDATE user SET created_at = :created_at WHERE id = :user_id"),
            {"created_at": created_at, "user_id": row["id"]},
        )


def _add_user_created_at() -> None:
    if not _table_exists("user"):
        return
    columns = _existing_columns("user")
    if "created_at" not in columns:
        op.add_column("user", sa.Column("created_at", sa.DateTime(), nullable=True))
    _backfill_user_created_at()
    with op.batch_alter_table("user") as batch_op:
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            nullable=False,
        )
    if "ix_user_created_at" not in _existing_indexes("user"):
        op.create_index("ix_user_created_at", "user", ["created_at"])


def _create_preauthorization_table() -> None:
    if _table_exists("clockinpreauthorization"):
        return
    op.create_table(
        "clockinpreauthorization",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("out_register_no", sa.String(length=255), nullable=False),
        sa.Column("authorized_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("used_target_type", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "target_date",
            "target_type",
            name="uq_clockinpreauthorization_target",
        ),
    )
    op.create_index(
        "ix_clockinpreauthorization_user_date",
        "clockinpreauthorization",
        ["tenant_id", "user_id", "target_date"],
    )
    op.create_index(
        "ix_clockinpreauthorization_user_status_date",
        "clockinpreauthorization",
        ["tenant_id", "user_id", "status", "target_date"],
    )


def upgrade() -> None:
    _add_user_created_at()
    _create_preauthorization_table()


def downgrade() -> None:
    if _table_exists("clockinpreauthorization"):
        op.drop_table("clockinpreauthorization")
    if _table_exists("user") and "created_at" in _existing_columns("user"):
        if "ix_user_created_at" in _existing_indexes("user"):
            op.drop_index("ix_user_created_at", table_name="user")
        with op.batch_alter_table("user") as batch_op:
            batch_op.drop_column("created_at")
