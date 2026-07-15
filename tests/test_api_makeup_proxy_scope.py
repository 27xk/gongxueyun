import datetime
import unittest
from unittest.mock import patch

from server import api
from server.models import User


def _config_data():
    return {
        "config": {
            "clockIn": {"schedule": {}},
            "reportSettings": {},
            "pushNotifications": [],
        },
        "userInfo": {"token": "token", "expiredTime": "1893456000"},
        "planInfo": {"planId": "plan-1"},
    }


class FakeApiClient:
    instances = []

    def __init__(self, config):
        self.config = config
        self.proxy_enabled = False
        FakeApiClient.instances.append(self)

    def enable_proxy(self):
        self.proxy_enabled = True

    def get_checkin_records(self, start_date, end_date):
        return []


class ApiMakeupProxyScopeTest(unittest.TestCase):
    def setUp(self):
        FakeApiClient.instances = []

    def test_manual_makeup_enables_proxy_after_runtime_refresh(self):
        user = User(id=7, phone="13800000000", password="encrypted")

        def ensure_runtime(client, config):
            self.assertFalse(client.proxy_enabled)

        def do_makeup(
            client,
            config,
            target_date,
            target_type=None,
            preauthorization_hooks=None,
        ):
            self.assertTrue(client.proxy_enabled)
            self.assertIsNotNone(preauthorization_hooks)
            return {"status": "success", "task_type": "makeup", "message": "ok"}

        with (
            patch.object(api, "ApiClient", FakeApiClient),
            patch.object(api, "user_to_config", return_value=_config_data()),
            patch.object(api, "_ensure_remote_runtime", side_effect=ensure_runtime),
            patch.object(api, "perform_clock_in_makeup", side_effect=do_makeup) as makeup,
            patch.object(api, "apply_execution_results_to_user"),
        ):
            result, _ = api._makeup_clockin_for_user(user, ["2026-05-22"], "START")

        self.assertEqual(result["status"], "success")
        makeup.assert_called_once()
        self.assertTrue(FakeApiClient.instances[0].proxy_enabled)

    def test_missing_clockin_query_does_not_enable_proxy(self):
        user = User(phone="13800000000", password="encrypted")

        def ensure_runtime(client, config):
            self.assertFalse(client.proxy_enabled)

        with (
            patch.object(api, "ApiClient", FakeApiClient),
            patch.object(api, "user_to_config", return_value=_config_data()),
            patch.object(api, "_ensure_remote_runtime", side_effect=ensure_runtime),
            patch.object(
                api,
                "_clockin_period_range",
                return_value=(datetime.date(2026, 5, 1), datetime.date(2026, 5, 2)),
            ),
        ):
            result = api._get_missing_clockin_days_for_user(user)

        self.assertTrue(result["ok"])
        self.assertFalse(FakeApiClient.instances[0].proxy_enabled)


if __name__ == "__main__":
    unittest.main()
