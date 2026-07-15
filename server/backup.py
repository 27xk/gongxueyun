import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlmodel import Session, SQLModel, select

from server.models import (
    AdminUser,
    AppUser,
    AuditLog,
    BatchJob,
    BatchJobItem,
    ClockInPreauthorization,
    HttpRequestMetric,
    SystemSetting,
    TaskExecutionEvent,
    TaskExecutionLock,
    Tenant,
    User,
)
from server.security import env_flag, is_production

BACKUP_FORMAT = "automoguding-backup-v1"
ENCRYPTED_BACKUP_FORMAT = "automoguding-backup-v1-encrypted"
BACKUP_KDF_ITERATIONS = 390_000

BACKUP_MODELS = [
    Tenant,
    AdminUser,
    AppUser,
    User,
    ClockInPreauthorization,
    AuditLog,
    BatchJob,
    BatchJobItem,
    SystemSetting,
    TaskExecutionLock,
    TaskExecutionEvent,
    HttpRequestMetric,
]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _b64encode(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64decode(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(str(value or "").encode("ascii"))


def _backup_encryption_key(explicit_key: str | None = None) -> str:
    return str(explicit_key or os.getenv("BACKUP_ENCRYPTION_KEY") or "").strip()


def _allow_plaintext_backup() -> bool:
    return env_flag("ALLOW_PLAINTEXT_BACKUP")


def _require_export_encryption(secret: str) -> None:
    if is_production() and not secret and not _allow_plaintext_backup():
        raise ValueError("backup encryption key is required in production")


def _fernet_for_key(secret: str, salt: bytes) -> Fernet:
    if not secret:
        raise ValueError("backup encryption key is required")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=BACKUP_KDF_ITERATIONS,
    )
    return Fernet(_b64encode(kdf.derive(secret.encode("utf-8"))))


def _encrypt_payload(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    salt = os.urandom(16)
    token = _fernet_for_key(secret, salt).encrypt(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return {
        "format": ENCRYPTED_BACKUP_FORMAT,
        "kdf": "pbkdf2_sha256",
        "iterations": BACKUP_KDF_ITERATIONS,
        "salt": _b64encode(salt),
        "payload": token.decode("ascii"),
    }


def _decrypt_payload(wrapper: dict[str, Any], secret: str) -> dict[str, Any]:
    if not secret:
        raise ValueError("backup encryption key is required")
    try:
        plaintext = _fernet_for_key(secret, _b64decode(str(wrapper.get("salt") or ""))).decrypt(
            str(wrapper.get("payload") or "").encode("ascii")
        )
    except (InvalidToken, ValueError) as exc:
        raise ValueError("backup decryption failed") from exc
    return json.loads(plaintext.decode("utf-8"))


def _manifest_for_tables(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    table_counts = {table: len(rows) for table, rows in sorted(tables.items())}
    table_checksums = {table: _checksum(rows) for table, rows in sorted(tables.items())}
    return {
        "table_counts": table_counts,
        "table_checksums": table_checksums,
        "checksum": _checksum({"table_counts": table_counts, "table_checksums": table_checksums}),
    }


def _verify_manifest(payload: dict[str, Any]) -> None:
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("backup manifest is missing")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("backup tables are missing")
    expected = _manifest_for_tables(tables)
    if manifest.get("table_counts") != expected["table_counts"]:
        raise ValueError("backup table counts checksum mismatch")
    if manifest.get("table_checksums") != expected["table_checksums"]:
        raise ValueError("backup table checksum mismatch")
    if manifest.get("checksum") != expected["checksum"]:
        raise ValueError("backup manifest checksum mismatch")


def _table_name(model: type[SQLModel]) -> str:
    return model.__tablename__


def _dump_row(row: SQLModel) -> dict[str, Any]:
    return row.model_dump(mode="json")


def export_database_json(db_engine, output_path: str | Path, *, encryption_key: str | None = None) -> dict[str, Any]:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"format": BACKUP_FORMAT, "tables": {}}
    summary: dict[str, Any] = {"path": str(path), "tables": {}, "encrypted": False}

    with Session(db_engine) as session:
        for model in BACKUP_MODELS:
            table = _table_name(model)
            rows = [_dump_row(row) for row in session.exec(select(model)).all()]
            payload["tables"][table] = rows
            summary["tables"][table] = len(rows)
    payload["manifest"] = _manifest_for_tables(payload["tables"])
    summary["checksum"] = payload["manifest"]["checksum"]

    secret = _backup_encryption_key(encryption_key)
    _require_export_encryption(secret)
    output_payload = _encrypt_payload(payload, secret) if secret else payload
    summary["encrypted"] = bool(secret)
    path.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def import_database_json(
    db_engine,
    input_path: str | Path,
    *,
    replace_existing: bool = False,
    encryption_key: str | None = None,
) -> dict[str, Any]:
    path = Path(input_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    encrypted = payload.get("format") == ENCRYPTED_BACKUP_FORMAT
    if encrypted:
        payload = _decrypt_payload(payload, _backup_encryption_key(encryption_key))
    if payload.get("format") != BACKUP_FORMAT:
        raise ValueError("unsupported backup format")
    _verify_manifest(payload)

    SQLModel.metadata.create_all(db_engine)
    summary: dict[str, Any] = {"path": str(path), "tables": {}, "encrypted": encrypted}

    with Session(db_engine) as session:
        if replace_existing:
            for model in reversed(BACKUP_MODELS):
                for row in session.exec(select(model)).all():
                    session.delete(row)
            session.commit()

        for model in BACKUP_MODELS:
            table = _table_name(model)
            rows = payload.get("tables", {}).get(table, [])
            for item in rows:
                session.add(model.model_validate(item))
            summary["tables"][table] = len(rows)
        session.commit()

    return summary
