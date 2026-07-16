import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


class MigrationsContractTest(unittest.TestCase):
    def _run_alembic(self, root: Path, db_path: Path, *args: str) -> None:
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite:///{db_path}"
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_alembic_files_exist(self):
        root = Path(__file__).resolve().parents[1]

        self.assertTrue((root / "alembic.ini").exists())
        self.assertTrue((root / "server" / "migrations" / "env.py").exists())
        versions = list((root / "server" / "migrations" / "versions").glob("*.py"))
        self.assertTrue(versions)

    def test_baseline_revision_imports(self):
        root = Path(__file__).resolve().parents[1]
        versions = list((root / "server" / "migrations" / "versions").glob("*.py"))
        baseline = next(path for path in versions if path.name == "20260527_0001_baseline.py")
        spec = importlib.util.spec_from_file_location("baseline_migration", baseline)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))

    def test_alembic_env_loads_project_dotenv(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "server" / "migrations" / "env.py").read_text(encoding="utf-8")

        self.assertIn("load_dotenv", source)
        self.assertIn('PROJECT_ROOT / ".env"', source)

    def test_baseline_migration_caches_schema_introspection(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "server" / "migrations" / "versions" / "20260527_0001_baseline.py"
        ).read_text(encoding="utf-8")

        self.assertIn("_SCHEMA_CACHE", source)
        self.assertIn("def _reset_schema_cache", source)
        self.assertIn("_reset_schema_cache()", source)
        self.assertIn("op.create_table(*args, **kwargs)", source)
        self.assertNotIn("_create_table(*args, **kwargs)\n    _reset_schema_cache()", source)

    def test_upgrade_existing_legacy_user_table_adds_runtime_columns(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            engine = create_engine(f"sqlite:///{db_path}")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE user ("
                        "id INTEGER PRIMARY KEY, "
                        "phone VARCHAR(255) NOT NULL, "
                        "password TEXT NOT NULL"
                        ")"
                    )
                )
            engine.dispose()

            self._run_alembic(root, db_path, "upgrade", "head")

            columns = {item["name"] for item in inspect(engine).get_columns("user")}
            tables = set(inspect(engine).get_table_names())
            preauthorization_columns = {
                item["name"] for item in inspect(engine).get_columns("clockinpreauthorization")
            }
            engine.dispose()

            self.assertIn("tenant", tables)
            self.assertIn("userInfo", columns)
            self.assertIn("planInfo", columns)
            self.assertIn("tenant_id", columns)
            self.assertIn("deleted_at", columns)
            self.assertIn("deleted_by", columns)
            self.assertIn("delete_reason", columns)
            self.assertIn("created_at", columns)
            self.assertIn("clockinpreauthorization", tables)
            self.assertIn("out_register_no", preauthorization_columns)
            self.assertIn("target_date", preauthorization_columns)
            self.assertIn("target_type", preauthorization_columns)

    def test_upgrade_previous_head_adds_clockin_preauthorization_schema(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "previous-head.db"
            self._run_alembic(root, db_path, "upgrade", "20260530_0002")
            self._run_alembic(root, db_path, "upgrade", "head")

            engine = create_engine(f"sqlite:///{db_path}")
            inspector = inspect(engine)
            user_columns = {item["name"] for item in inspector.get_columns("user")}
            tables = set(inspector.get_table_names())
            with engine.connect() as conn:
                revision = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            engine.dispose()

            self.assertIn("created_at", user_columns)
            self.assertIn("clockinpreauthorization", tables)
            self.assertEqual(revision, "20260715_0003")

    def test_mfa_removal_revision_exists(self):
        root = Path(__file__).resolve().parents[1]
        migration = root / "server" / "migrations" / "versions" / "20260530_0002_remove_admin_mfa.py"

        self.assertTrue(migration.exists())
        source = migration.read_text(encoding="utf-8")
        self.assertIn('down_revision = "20260527_0001"', source)

    def test_clockin_preauthorization_revision_exists(self):
        root = Path(__file__).resolve().parents[1]
        migration = (
            root
            / "server"
            / "migrations"
            / "versions"
            / "20260715_0003_clockin_preauthorization.py"
        )

        self.assertTrue(migration.exists())
        source = migration.read_text(encoding="utf-8")
        self.assertIn('down_revision = "20260530_0002"', source)

    def test_upgrade_existing_admin_table_drops_legacy_mfa_columns(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy-admin.db"
            engine = create_engine(f"sqlite:///{db_path}")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE adminuser ("
                        "id INTEGER PRIMARY KEY, "
                        "username VARCHAR(255) NOT NULL, "
                        "password_hash TEXT NOT NULL, "
                        "mfa_enabled BOOLEAN NOT NULL DEFAULT 0, "
                        "mfa_totp_secret TEXT NULL, "
                        "mfa_confirmed_at DATETIME NULL"
                        ")"
                    )
                )
                conn.execute(text("CREATE INDEX ix_adminuser_mfa_enabled ON adminuser (mfa_enabled)"))
                conn.execute(text("CREATE INDEX ix_adminuser_mfa_confirmed_at ON adminuser (mfa_confirmed_at)"))
            engine.dispose()

            self._run_alembic(root, db_path, "upgrade", "head")

            inspector = inspect(engine)
            columns = {item["name"] for item in inspector.get_columns("adminuser")}
            indexes = {item["name"] for item in inspector.get_indexes("adminuser")}
            engine.dispose()
            self.assertNotIn("mfa_enabled", columns)
            self.assertNotIn("mfa_totp_secret", columns)
            self.assertNotIn("mfa_confirmed_at", columns)
            self.assertNotIn("ix_adminuser_mfa_enabled", indexes)
            self.assertNotIn("ix_adminuser_mfa_confirmed_at", indexes)


if __name__ == "__main__":
    unittest.main()
