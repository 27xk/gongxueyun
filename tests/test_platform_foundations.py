import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import Session, SQLModel, create_engine, select


ROOT = Path(__file__).resolve().parents[1]


class PlatformFoundationsTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        import server.models  # noqa: F401 - load SQLModel metadata before create_all

        SQLModel.metadata.create_all(self.engine)

    def test_default_tenant_is_created_and_models_are_tenant_scoped(self):
        from server.database import ensure_default_tenant
        from server.models import AdminUser, AuditLog, BatchJob, Tenant, User

        ensure_default_tenant(self.engine)

        with Session(self.engine) as session:
            tenant = session.get(Tenant, "default")
            self.assertIsNotNone(tenant)
            self.assertEqual(tenant.status, "active")

            user = User(phone="13800000000", password="encrypted")
            admin = AdminUser(username="admin", password_hash="hash")
            audit = AuditLog(actor="admin", action="probe", detail={})
            batch = BatchJob(created_by="admin")
            session.add(user)
            session.add(admin)
            session.add(audit)
            session.add(batch)
            session.commit()

            self.assertEqual(user.tenant_id, "default")
            self.assertEqual(admin.tenant_id, "default")
            self.assertEqual(audit.tenant_id, "default")
            self.assertEqual(batch.tenant_id, "default")

    def test_ensure_tenant_columns_adds_columns_to_legacy_tables(self):
        from server.database import ensure_tenant_columns

        legacy_engine = create_engine("sqlite://")
        with legacy_engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE user (id INTEGER PRIMARY KEY, phone TEXT, password TEXT)")
            conn.exec_driver_sql("CREATE TABLE auditlog (id INTEGER PRIMARY KEY, actor TEXT, action TEXT)")

        ensure_tenant_columns(legacy_engine)

        inspector = inspect(legacy_engine)
        self.assertIn("tenant_id", {item["name"] for item in inspector.get_columns("user")})
        self.assertIn("tenant_id", {item["name"] for item in inspector.get_columns("auditlog")})

    def test_runtime_schema_mutation_is_disabled_by_default_in_production(self):
        from server.database import should_run_runtime_schema_migrations

        with patch.dict("os.environ", {"APP_ENV": "production"}, clear=False):
            self.assertFalse(should_run_runtime_schema_migrations())

        with patch.dict("os.environ", {"APP_ENV": "production", "ALLOW_RUNTIME_SCHEMA_MIGRATIONS": "true"}, clear=False):
            self.assertTrue(should_run_runtime_schema_migrations())

        with patch.dict("os.environ", {"APP_ENV": "development"}, clear=False):
            self.assertTrue(should_run_runtime_schema_migrations())

    def test_database_schema_must_be_current_when_runtime_migrations_are_disabled(self):
        from server.database import require_database_schema_current

        with self.assertRaises(RuntimeError):
            require_database_schema_current(self.engine)

    def test_startup_checks_schema_when_runtime_migrations_are_disabled(self):
        from server import main

        with patch("server.main.should_run_runtime_schema_migrations", return_value=False), \
            patch("server.main.require_database_schema_current") as require_current, \
            patch("server.main.ensure_seed_admin_users"), \
            patch("server.main._should_auto_download_captcha_models", return_value=False), \
            patch("server.main.should_start_background_services", return_value=False):
            main.on_startup()

        require_current.assert_called_once()

    def test_mysql_engine_uses_explicit_pool_settings(self):
        from server.database import _engine_options

        with patch.dict(
            "os.environ",
            {
                "DATABASE_POOL_SIZE": "7",
                "DATABASE_MAX_OVERFLOW": "3",
                "DATABASE_POOL_RECYCLE_SECONDS": "600",
                "DATABASE_POOL_TIMEOUT_SECONDS": "9",
            },
            clear=False,
        ):
            options = _engine_options("mysql+pymysql://user:pass@db/app")

        self.assertEqual(options["pool_size"], 7)
        self.assertEqual(options["max_overflow"], 3)
        self.assertEqual(options["pool_recycle"], 600)
        self.assertEqual(options["pool_timeout"], 9)
        self.assertTrue(options["pool_pre_ping"])

    def test_seed_admin_users_keeps_existing_operator_enabled(self):
        from server.admin_users import ensure_seed_admin_users
        from server.models import AdminUser

        with patch.dict(
            "os.environ",
            {
                "APP_ENV": "development",
                "ADMIN_USERNAME": "root",
                "ADMIN_PASSWORD": "root-password-12345",
                "APP_SECRET": "test-secret-value-with-more-than-thirty-two-chars",
            },
            clear=False,
        ):
            with Session(self.engine) as session:
                session.add(AdminUser(tenant_id="default", username="operator", password_hash="hash", role="operator", enabled=True))
                session.commit()

            with patch("server.database.engine", self.engine):
                ensure_seed_admin_users()

            with Session(self.engine) as session:
                operator = session.exec(select(AdminUser).where(AdminUser.username == "operator")).one()
                seeded = session.exec(select(AdminUser).where(AdminUser.username == "root")).one()

        self.assertTrue(operator.enabled)
        self.assertEqual(operator.role, "operator")
        self.assertEqual(seeded.role, "admin")

    def test_require_permission_dependency_blocks_underprivileged_roles(self):
        from server.auth import get_auth_payload, require_permission

        app = FastAPI()

        @app.get("/probe")
        def probe(payload: dict = Depends(require_permission("audit:purge"))):
            return {"sub": payload["sub"]}

        app.dependency_overrides[get_auth_payload] = lambda: {"sub": "viewer", "role": "viewer"}

        response = TestClient(app).get("/probe")

        self.assertEqual(response.status_code, 403)

    def test_request_metrics_capture_status_and_latency(self):
        from server.observability import record_http_request, runtime_metrics

        record_http_request(
            method="GET",
            path="/api/users",
            status_code=200,
            duration_ms=12,
            request_id="req-123",
            db_engine=self.engine,
        )
        record_http_request(method="POST", path="/api/users", status_code=403, duration_ms=5, db_engine=self.engine)

        metrics = runtime_metrics(db_engine=self.engine)

        self.assertEqual(metrics["http_requests"]["total"], 2)
        self.assertEqual(metrics["http_requests"]["by_status"]["2xx"], 1)
        self.assertEqual(metrics["http_requests"]["by_status"]["4xx"], 1)
        self.assertGreaterEqual(metrics["http_requests"]["latency_ms"]["max"], 12)
        self.assertEqual(metrics["http_requests"]["last_request_id"], "req-123")

    def test_task_event_inherits_request_id_context(self):
        from server.observability import record_task_event, runtime_metrics
        from server.request_context import request_context

        with request_context("trace-abc"):
            record_task_event(
                source="api",
                event="run",
                task_key="manual:user:1",
                status="success",
                db_engine=self.engine,
            )

        metrics = runtime_metrics(db_engine=self.engine)

        self.assertEqual(metrics["task_events"]["last_request_id"], "trace-abc")

    def test_database_backup_exports_and_imports_core_tables(self):
        from server.backup import export_database_json, import_database_json
        from server.database import ensure_default_tenant
        from server.models import Tenant, User

        ensure_default_tenant(self.engine)
        with Session(self.engine) as session:
            session.add(User(phone="13800000000", password="encrypted", remark="backup"))
            session.commit()

        with tempfile.TemporaryDirectory() as tmp:
            backup_path = Path(tmp) / "backup.json"
            summary = export_database_json(self.engine, backup_path)

            self.assertEqual(summary["tables"]["user"], 1)
            payload = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], "automoguding-backup-v1")
            self.assertIn("manifest", payload)
            self.assertIn("table_checksums", payload["manifest"])

            restored_engine = create_engine("sqlite://")
            SQLModel.metadata.create_all(restored_engine)
            restore_summary = import_database_json(restored_engine, backup_path)

            with Session(restored_engine) as session:
                self.assertEqual(session.get(Tenant, "default").status, "active")
                restored_user = session.exec(select(User).where(User.phone == "13800000000")).one()
                self.assertEqual(restored_user.remark, "backup")
            self.assertEqual(restore_summary["tables"]["user"], 1)

    def test_database_backup_rejects_tampered_payload(self):
        from server.backup import export_database_json, import_database_json
        from server.models import User

        with Session(self.engine) as session:
            session.add(User(phone="13800000000", password="encrypted", remark="before"))
            session.commit()

        with tempfile.TemporaryDirectory() as tmp:
            backup_path = Path(tmp) / "backup.json"
            export_database_json(self.engine, backup_path)
            payload = json.loads(backup_path.read_text(encoding="utf-8"))
            payload["tables"]["user"][0]["remark"] = "tampered"
            backup_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            restored_engine = create_engine("sqlite://")
            SQLModel.metadata.create_all(restored_engine)
            with self.assertRaises(ValueError):
                import_database_json(restored_engine, backup_path)

    def test_database_backup_can_be_encrypted_and_restored(self):
        from server.backup import export_database_json, import_database_json
        from server.database import ensure_default_tenant
        from server.models import User

        ensure_default_tenant(self.engine)
        with Session(self.engine) as session:
            session.add(User(phone="13900000000", password="encrypted", remark="encrypted-backup"))
            session.commit()

        with tempfile.TemporaryDirectory() as tmp:
            backup_path = Path(tmp) / "backup.enc.json"
            summary = export_database_json(self.engine, backup_path, encryption_key="backup-key-for-tests")
            raw = backup_path.read_text(encoding="utf-8")
            self.assertTrue(summary["encrypted"])
            self.assertIn("automoguding-backup-v1-encrypted", raw)
            self.assertNotIn("13900000000", raw)
            self.assertNotIn("encrypted-backup", raw)

            restored_engine = create_engine("sqlite://")
            import_database_json(restored_engine, backup_path, encryption_key="backup-key-for-tests")
            with Session(restored_engine) as session:
                restored_user = session.exec(select(User).where(User.phone == "13900000000")).one()
                self.assertEqual(restored_user.remark, "encrypted-backup")

    def test_encrypted_database_backup_requires_key(self):
        from server.backup import export_database_json, import_database_json
        from server.database import ensure_default_tenant

        ensure_default_tenant(self.engine)
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = Path(tmp) / "backup.enc.json"
            export_database_json(self.engine, backup_path, encryption_key="backup-key-for-tests")

            restored_engine = create_engine("sqlite://")
            with self.assertRaises(ValueError):
                import_database_json(restored_engine, backup_path)

    def test_production_database_backup_requires_encryption_unless_explicitly_allowed(self):
        from server.backup import export_database_json
        from server.database import ensure_default_tenant

        ensure_default_tenant(self.engine)
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = Path(tmp) / "backup.json"
            with patch.dict(
                "os.environ",
                {
                    "APP_ENV": "production",
                    "BACKUP_ENCRYPTION_KEY": "",
                    "ALLOW_PLAINTEXT_BACKUP": "",
                },
                clear=False,
            ):
                with self.assertRaises(ValueError):
                    export_database_json(self.engine, backup_path)

            with patch.dict(
                "os.environ",
                {
                    "APP_ENV": "production",
                    "BACKUP_ENCRYPTION_KEY": "",
                    "ALLOW_PLAINTEXT_BACKUP": "true",
                },
                clear=False,
            ):
                summary = export_database_json(self.engine, backup_path)

        self.assertFalse(summary["encrypted"])

    def test_production_database_backup_uses_environment_encryption_key(self):
        from server.backup import export_database_json
        from server.database import ensure_default_tenant
        from server.models import User

        ensure_default_tenant(self.engine)
        with Session(self.engine) as session:
            session.add(User(phone="13700000000", password="encrypted", remark="prod-backup"))
            session.commit()

        with tempfile.TemporaryDirectory() as tmp:
            backup_path = Path(tmp) / "backup.enc.json"
            with patch.dict(
                "os.environ",
                {
                    "APP_ENV": "production",
                    "BACKUP_ENCRYPTION_KEY": "backup-key-from-env-for-tests",
                    "ALLOW_PLAINTEXT_BACKUP": "",
                },
                clear=False,
            ):
                summary = export_database_json(self.engine, backup_path)

            raw = backup_path.read_text(encoding="utf-8")

        self.assertTrue(summary["encrypted"])
        self.assertIn("automoguding-backup-v1-encrypted", raw)
        self.assertNotIn("13700000000", raw)
        self.assertNotIn("prod-backup", raw)

    def test_compose_worker_services_have_ready_healthcheck(self):
        for compose_name in ("docker-compose.yml", "docker-compose.image.yml"):
            compose = (ROOT / compose_name).read_text(encoding="utf-8")
            worker_block = compose.split("\n  worker:", 1)[1]

            self.assertIn("healthcheck:", worker_block, compose_name)
            self.assertIn("127.0.0.1:8147/readyz", worker_block, compose_name)

    def test_production_compose_defaults_secure_cookie_and_hsts(self):
        for compose_name in ("docker-compose.yml", "docker-compose.image.yml"):
            compose = (ROOT / compose_name).read_text(encoding="utf-8")

            self.assertIn("AUTH_COOKIE_SECURE: ${AUTH_COOKIE_SECURE:-true}", compose, compose_name)
            self.assertIn("ENABLE_HSTS: ${ENABLE_HSTS:-true}", compose, compose_name)

    def test_ci_runs_dependency_and_image_security_scans(self):
        workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

        self.assertIn("pip-audit", workflow)
        self.assertIn("npm audit", workflow)
        self.assertIn("aquasecurity/trivy-action", workflow)
        self.assertIn("aquasecurity/trivy-action@57a97c7e7821a5776cebc9bb87c984fa69cba8f1", workflow)
        self.assertNotIn("aquasecurity/setup-trivy@v0.2.1", workflow)
        self.assertNotIn("aquasecurity/trivy-action@915b19bbe73b92a6cf82a1bc12b087c9a19a5fe2", workflow)

    def test_backend_requirements_do_not_pin_vulnerable_python_multipart(self):
        requirements = (ROOT / "server" / "requirements.txt").read_text(encoding="utf-8").splitlines()
        version = None
        for line in requirements:
            if line.startswith("python-multipart=="):
                version = tuple(int(part) for part in line.split("==", 1)[1].split("."))
                break

        self.assertIsNotNone(version)
        self.assertGreaterEqual(version, (0, 0, 29))
        self.assertNotIn("python-multipart==0.0.9", "\n".join(requirements))

    def test_frontend_lockfile_uses_non_vulnerable_vite(self):
        package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))
        vite_spec = package["devDependencies"]["vite"]
        vite_lock = lock["packages"][""]["devDependencies"]["vite"]
        vite_version = tuple(int(part) for part in lock["packages"]["node_modules/vite"]["version"].split("."))

        self.assertEqual(vite_spec, "^7.3.2")
        self.assertEqual(vite_lock, "^7.3.2")
        self.assertGreaterEqual(vite_version, (7, 3, 2))
        self.assertNotIn('"version": "7.3.1"', (ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))

    def test_frontend_lockfile_uses_non_vulnerable_lodash(self):
        package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))

        for dependency in ("lodash", "lodash-es"):
            override = package["overrides"][dependency]
            locked_version = lock["packages"][f"node_modules/{dependency}"]["version"]
            version = tuple(int(part) for part in locked_version.split("."))

            self.assertEqual(override, "4.18.1")
            self.assertGreaterEqual(version, (4, 18, 1))
            self.assertNotEqual(locked_version, "4.17.23")

    def test_frontend_lockfile_uses_non_vulnerable_axios(self):
        package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))
        axios_spec = package["dependencies"]["axios"]
        axios_lock = lock["packages"][""]["dependencies"]["axios"]
        axios_version = tuple(int(part) for part in lock["packages"]["node_modules/axios"]["version"].split("."))

        self.assertEqual(axios_spec, "^1.15.2")
        self.assertEqual(axios_lock, "^1.15.2")
        self.assertGreaterEqual(axios_version, (1, 15, 2))
        self.assertNotIn('"version": "1.13.2"', (ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))

    def test_ci_generates_signed_supply_chain_artifacts(self):
        workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

        self.assertIn("id-token: write", workflow)
        self.assertIn("permissions:\n      contents: read", workflow)
        self.assertIn("sbom: true", workflow)
        self.assertIn("provenance: true", workflow)
        self.assertIn("sigstore/cosign-installer", workflow)
        self.assertIn("cosign sign --yes", workflow)
        self.assertIn("cosign verify", workflow)

    def test_docker_and_actions_are_pinned_for_supply_chain(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

        self.assertIn("FROM node:20-alpine@", dockerfile)
        self.assertIn("FROM python:3.10-slim@", dockerfile)
        self.assertNotIn("ACTIONS_ALLOW_UNPINNED", workflow)
        self.assertIn("Verify action pinning", workflow)

    def test_ci_runs_frontend_quality_gates(self):
        workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")
        package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))

        self.assertIn("npm run lint", workflow)
        self.assertIn("npm test", workflow)
        self.assertIn("frontend_quality_gate.mjs", package["scripts"]["lint"])
        self.assertIn("test:static", package["scripts"]["test"])

    def test_ci_runs_backup_restore_drill(self):
        workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")
        script = (ROOT / "scripts" / "backup_restore_drill.py").read_text(encoding="utf-8")

        self.assertIn("python scripts/backup_restore_drill.py", workflow)
        self.assertIn("import_database_json", script)
        self.assertIn("export_database_json", script)

    def test_user_list_read_uses_default_factory_for_logs(self):
        source = (ROOT / "server" / "models.py").read_text(encoding="utf-8")

        self.assertIn("logs: List[str] = Field(default_factory=list)", source)
        self.assertNotIn("logs: List[str] = []", source)

    def test_tenant_management_creates_and_disables_tenant(self):
        from server import api
        from server.models import AuditLog, Tenant

        with Session(self.engine) as session:
            created = api.create_tenant(
                session=session,
                admin={"sub": "admin", "role": "admin", "tenant_id": "default"},
                req=api.TenantCreateRequest(id="acme", name="Acme Inc"),
            )
            self.assertEqual(created["id"], "acme")
            page = api.read_tenants_page(
                session=session,
                admin={"sub": "admin", "role": "admin", "tenant_id": "default"},
            )
            self.assertEqual(page["total"], 1)

            updated = api.update_tenant(
                session=session,
                admin={"sub": "admin", "role": "admin", "tenant_id": "default"},
                tenant_id="acme",
                req=api.TenantUpdateRequest(status="disabled"),
            )

            self.assertEqual(updated["status"], "disabled")
            self.assertEqual(session.get(Tenant, "acme").status, "disabled")
            self.assertEqual(len(session.exec(select(AuditLog).where(AuditLog.action == "tenant.update")).all()), 1)

    def test_admin_management_can_create_operator_and_viewer_accounts(self):
        from server import api
        from server.models import AdminUser, AuditLog

        with Session(self.engine) as session:
            created = api.create_admin_user(
                session=session,
                admin={"sub": "admin", "role": "admin", "tenant_id": "default"},
                req=api.AdminUserCreateRequest(
                    username="operator",
                    password="Op3rator-Secure-Key",
                    role="operator",
                ),
            )

            self.assertEqual(created["username"], "operator")
            self.assertEqual(created["role"], "operator")
            user = session.exec(select(AdminUser).where(AdminUser.username == "operator")).one()
            self.assertTrue(user.enabled)
            self.assertEqual(user.role, "operator")
            self.assertTrue(user.password_hash.startswith("pbkdf2_sha256$"))
            self.assertEqual(len(session.exec(select(AuditLog).where(AuditLog.action == "admin_user.create")).all()), 1)

    def test_admin_management_rejects_invalid_role_and_duplicate_username(self):
        from server import api
        from server.models import AdminUser
        from server.auth import hash_password

        with Session(self.engine) as session:
            session.add(AdminUser(username="operator", password_hash=hash_password("Op3rator-Secure-Key"), role="operator"))
            session.commit()

            with self.assertRaises(Exception):
                api.create_admin_user(
                    session=session,
                    admin={"sub": "admin", "role": "admin", "tenant_id": "default"},
                    req=api.AdminUserCreateRequest(
                        username="operator",
                        password="Op3rator-Secure-Key",
                        role="operator",
                    ),
                )

            with self.assertRaises(Exception):
                api.create_admin_user(
                    session=session,
                    admin={"sub": "admin", "role": "admin", "tenant_id": "default"},
                    req=api.AdminUserCreateRequest(
                        username="hacker",
                        password="Op3rator-Secure-Key",
                        role="owner",
                    ),
                )

    def test_quality_gate_flags_bare_except_pass_and_server_print(self):
        import scripts.quality_gate as quality_gate

        offenders = [
            Path("server/example.py"),
            Path("server/nested/example.py"),
            Path("server/api.py"),
            Path("scripts/quality_gate.py"),
            Path("web/src/invite.js"),
        ]

        def fake_files():
            return offenders[:4]

        def fake_source_files():
            return [offenders[4]]

        def fake_read(path: Path) -> str:
            if path.as_posix() == "server/example.py":
                return "try:\n    work()\nexcept Exception:\n    pass\n"
            if path.as_posix() == "server/nested/example.py":
                return "print('debug')\n"
            if path.as_posix() == "server/api.py":
                return "user = _get_active_user_or_404(session, user_id)\n"
            if path.as_posix() == "web/src/invite.js":
                return "export const invitation = true\n"
            return ""

        old_files = quality_gate._python_files
        old_source_files = quality_gate._source_files_for_product_surface
        old_read = quality_gate._read
        try:
            quality_gate._python_files = fake_files
            quality_gate._source_files_for_product_surface = fake_source_files
            quality_gate._read = fake_read
            failures = (
                quality_gate.check_no_silent_exception_pass()
                + quality_gate.check_no_server_prints()
                + quality_gate.check_no_unscoped_user_lookup()
                + quality_gate.check_no_user_invitation_surface()
            )
        finally:
            quality_gate._python_files = old_files
            quality_gate._source_files_for_product_surface = old_source_files
            quality_gate._read = old_read

        self.assertTrue(any("bare except/pass" in item for item in failures))
        self.assertTrue(any("server-side print" in item for item in failures))
        self.assertTrue(any("unscoped user lookup" in item for item in failures))
        self.assertTrue(any("user invitation surface" in item for item in failures))

    def test_utc_now_is_timezone_aware(self):
        from server.time_utils import utc_now

        now = utc_now()

        self.assertIsNotNone(now.tzinfo)
        self.assertIsNotNone(now.utcoffset())


if __name__ == "__main__":
    unittest.main()
