import json
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "docs" / "api" / "openapi-contract.json"


class OpenAPIContractTest(unittest.TestCase):
    def test_contract_generator_rejects_unpinned_schema_dependencies(self):
        from scripts import openapi_contract

        installed = {
            "fastapi": "0.0.0",
            "starlette": "1.3.1",
            "sqlmodel": "0.0.37",
        }
        with (
            patch.object(
                openapi_contract.importlib_metadata,
                "version",
                side_effect=lambda package: installed[package],
            ),
            self.assertRaisesRegex(RuntimeError, "fastapi==0.136.3"),
        ):
            openapi_contract.require_contract_generator_environment()

    def test_openapi_contract_snapshot_is_current(self):
        from scripts.openapi_contract import build_openapi_contract
        from server.main import app

        self.assertTrue(SNAPSHOT_PATH.exists(), "OpenAPI contract snapshot is missing")
        expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        actual = build_openapi_contract(app.openapi())

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
