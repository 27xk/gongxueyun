import json
import sys
import tempfile
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.backup import export_database_json, import_database_json  # noqa: E402
from server.models import DEFAULT_TENANT_ID, Tenant, User  # noqa: E402


def run_drill() -> dict:
    source = create_engine("sqlite://")
    SQLModel.metadata.create_all(source)
    with Session(source) as session:
        session.add(Tenant(id=DEFAULT_TENANT_ID, name="Default Tenant", status="active"))
        session.add(User(phone="13800000000", password="encrypted-password", remark="restore-drill"))
        session.commit()

    with tempfile.TemporaryDirectory() as tmp:
        backup_path = Path(tmp) / "backup.enc.json"
        export_summary = export_database_json(source, backup_path, encryption_key="restore-drill-key")

        restored = create_engine("sqlite://")
        restore_summary = import_database_json(restored, backup_path, encryption_key="restore-drill-key")
        with Session(restored) as session:
            user = session.exec(select(User).where(User.phone == "13800000000")).one()
            tenant = session.get(Tenant, DEFAULT_TENANT_ID)

        if tenant is None or tenant.status != "active":
            raise RuntimeError("restored tenant validation failed")
        if user.remark != "restore-drill":
            raise RuntimeError("restored user validation failed")

    return {
        "ok": True,
        "encrypted": bool(export_summary.get("encrypted")),
        "exported_user_rows": export_summary["tables"]["user"],
        "restored_user_rows": restore_summary["tables"]["user"],
        "checksum": export_summary.get("checksum"),
    }


def main() -> int:
    print(json.dumps(run_drill(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
