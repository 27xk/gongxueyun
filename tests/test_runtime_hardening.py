import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server import auth
from server.coreApi.AiServiceClient import generate_article
from server.rate_limit import check_rate_limit, clear_memory_rate_limits
from server.util.Config import ConfigManager
from sqlmodel import SQLModel, create_engine


class FakeRequest:
    def __init__(self, host="10.0.0.5", forwarded_for="203.0.113.7"):
        self.client = SimpleNamespace(host=host)
        self.headers = {"x-forwarded-for": forwarded_for}


class FakeAIResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "local ai content"}}]}


class RuntimeHardeningTest(unittest.TestCase):
    def tearDown(self):
        auth._SECRET_CACHE = None
        clear_memory_rate_limits()

    def test_client_ip_ignores_forwarded_for_unless_proxy_headers_are_trusted(self):
        request = FakeRequest()

        with patch.dict("os.environ", {"TRUST_PROXY_HEADERS": "", "TRUSTED_PROXY_IPS": ""}, clear=False):
            self.assertEqual(auth.get_client_ip(request), "10.0.0.5")

    def test_client_ip_uses_forwarded_for_only_from_trusted_proxy(self):
        request = FakeRequest(host="10.0.0.5", forwarded_for="203.0.113.7, 10.0.0.5")

        with patch.dict("os.environ", {"TRUSTED_PROXY_IPS": "10.0.0.5"}, clear=False):
            self.assertEqual(auth.get_client_ip(request), "203.0.113.7")

    def test_production_ignores_trust_proxy_headers_without_explicit_trusted_proxy(self):
        request = FakeRequest(host="10.0.0.5", forwarded_for="203.0.113.7")

        with patch.dict("os.environ", {"APP_ENV": "production", "TRUST_PROXY_HEADERS": "true"}, clear=False):
            self.assertEqual(auth.get_client_ip(request), "10.0.0.5")

    def test_production_rejects_example_app_secret(self):
        auth._SECRET_CACHE = None

        with patch.dict(
            "os.environ",
            {"APP_ENV": "production", "APP_SECRET": "please-change-me-in-production"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                auth._secret()

    def test_generate_article_rejects_private_ai_url_by_default(self):
        config = ConfigManager(
            config={
                "config": {
                    "ai": {
                        "apikey": "test-key",
                        "apiUrl": "http://127.0.0.1:8080",
                        "model": "test-model",
                    }
                },
                "userInfo": {"orgJson": {"majorName": "test-major"}},
            }
        )

        with patch.dict("os.environ", {"ALLOW_PRIVATE_AI_ENDPOINTS": "", "AI_ALLOWED_HOSTS": ""}, clear=False):
            with self.assertRaises(ValueError):
                generate_article(config, "title", {}, count=10, max_retries=1, timeout=1)

    def test_generate_article_allows_private_ai_url_when_explicitly_opted_in(self):
        config = ConfigManager(
            config={
                "config": {
                    "ai": {
                        "apikey": "test-key",
                        "apiUrl": "http://127.0.0.1:8080",
                        "model": "test-model",
                    }
                },
                "userInfo": {"orgJson": {"majorName": "test-major"}},
            }
        )

        with (
            patch.dict(
                "os.environ",
                {"ALLOW_PRIVATE_AI_ENDPOINTS": "true", "AI_ALLOWED_HOSTS": "127.0.0.1"},
                clear=False,
            ),
            patch("server.coreApi.AiServiceClient.requests.post", return_value=FakeAIResponse()) as post,
        ):
            content = generate_article(config, "title", {}, count=10, max_retries=1, timeout=1)

        self.assertEqual(content, "local ai content")
        self.assertEqual(post.call_args.kwargs["url"], "http://127.0.0.1:8080/v1/chat/completions")

    def test_api_role_does_not_start_background_services(self):
        runtime_mode = importlib.import_module("server.runtime_mode")

        with patch.dict("os.environ", {"APP_ROLE": "api"}, clear=False):
            self.assertFalse(runtime_mode.should_start_background_services())

    def test_worker_role_starts_background_services(self):
        runtime_mode = importlib.import_module("server.runtime_mode")

        with patch.dict("os.environ", {"APP_ROLE": "worker"}, clear=False):
            self.assertTrue(runtime_mode.should_start_background_services())

    def test_database_rate_limiter_blocks_after_limit(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        with (
            patch.dict("os.environ", {"RATE_LIMIT_BACKEND": "database"}, clear=False),
            patch("server.database.engine", engine),
        ):
            check_rate_limit("login:10.0.0.5", limit=1, per_seconds=60)
            with self.assertRaises(Exception) as ctx:
                check_rate_limit("login:10.0.0.5", limit=1, per_seconds=60)

        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)

    def test_login_rate_limit_uses_ip_and_principal_buckets_without_raw_identifier(self):
        from server.api import _rate_limit_login_attempt

        keys = []

        def fake_rate_limit(key, limit, per_seconds, detail=None):
            keys.append(key)

        with patch("server.api._rate_limit", side_effect=fake_rate_limit):
            _rate_limit_login_attempt(
                scope="admin_login",
                client_ip="203.0.113.9",
                tenant_id="default",
                principal="Admin@Example.COM",
                ip_limit=10,
                principal_limit=5,
                per_seconds=60,
            )

        self.assertEqual(keys[0], "admin_login:ip:203.0.113.9")
        self.assertTrue(keys[1].startswith("admin_login:principal:default:"))
        self.assertNotIn("Admin@Example", keys[1])
        self.assertNotIn("admin@example", keys[1])

    def test_background_readiness_reflects_worker_components(self):
        import server.main as main

        with patch.dict("os.environ", {"APP_ROLE": "worker"}, clear=False):
            with (
                patch("server.main.is_scheduler_running", return_value=True),
                patch("server.main.is_queue_worker_running", return_value=True),
            ):
                self.assertTrue(main._background_services_ready())

            with (
                patch("server.main.is_scheduler_running", return_value=False),
                patch("server.main.is_queue_worker_running", return_value=True),
            ):
                self.assertFalse(main._background_services_ready())


if __name__ == "__main__":
    unittest.main()
