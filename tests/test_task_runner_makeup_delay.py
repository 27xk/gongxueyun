import unittest
from unittest.mock import Mock, patch

from server import task_runner


class MakeupBatchDelayTest(unittest.TestCase):
    def test_replace_clock_in_ignores_custom_day_skip(self):
        api_client = Mock()
        config = Mock()

        values = {
            "config.clockIn.location.address": "test address",
            "config.clockIn.mode": "custom",
            "config.clockIn.specialClockIn": False,
            "config.clockIn.customDays": [],
            "config.clockIn.imageCount": 0,
            "config.clockIn.description": [],
            "config.clockIn.latitude": "30.1",
            "config.clockIn.longitude": "120.1",
            "config.clockIn.device": "android",
            "userInfo.userId": "u1",
            "userInfo.nikeName": "tester",
            "userInfo.orgJson.snowFlakeId": "s1",
        }
        config.get_value.side_effect = lambda key: values.get(key)
        api_client.get_checkin_records.return_value = []
        api_client.get_upload_token.return_value = "upload-token"

        result = task_runner.perform_clock_in(
            api_client,
            config,
            forced_checkin_type="START",
            target_time=task_runner.datetime(2026, 5, 22, 7, 30),
            replace=True,
        )

        self.assertNotEqual(result["status"], "skip")
        api_client.submit_clock_in_replace.assert_called_once()

    def test_perform_clock_in_makeup_many_waits_between_dates(self):
        api_client = Mock()
        config = Mock()

        with patch.object(task_runner, "perform_clock_in_makeup") as makeup, patch.object(task_runner.time, "sleep") as sleep:
            makeup.side_effect = [
                {"status": "success", "message": "ok"},
                {"status": "success", "message": "ok"},
                {"status": "success", "message": "ok"},
            ]

            result = task_runner.perform_clock_in_makeup_many(
                api_client,
                config,
                ["2026-05-20", "2026-05-21", "2026-05-22"],
                target_type="START",
                delay_seconds=1.5,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["details"]["请求间隔秒"], 1.5)
        self.assertEqual(makeup.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_any_call(1.5)

    def test_perform_clock_in_makeup_many_retries_after_rate_limit(self):
        api_client = Mock()
        config = Mock()

        with patch.object(task_runner, "perform_clock_in_makeup") as makeup, patch.object(task_runner.time, "sleep") as sleep:
            makeup.side_effect = [
                {"status": "fail", "message": "打卡失败: IP请求过于频繁，请稍后再试:111.23.44.229", "task_type": "补卡"},
                {"status": "success", "message": "ok", "task_type": "补卡"},
            ]

            result = task_runner.perform_clock_in_makeup_many(
                api_client,
                config,
                ["2026-05-22"],
                target_type="START",
                delay_seconds=0,
                rate_limit_retries=2,
                rate_limit_retry_seconds=0.5,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["details"]["频繁重试次数"], 1)
        self.assertEqual(makeup.call_count, 2)
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
