import os
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from server import api
from server.util.Config import ConfigManager
from server.models import User


class ReportMakeupAllTest(unittest.TestCase):
    def test_makeup_all_reports_submits_each_missing_period_with_delay(self):
        user = User(phone="tester", password="secret")
        clients = [Mock(), Mock()]

        with (
            patch.object(
                api,
                "_get_missing_report_periods_for_user",
                return_value={"options": [{"value": "2026-05-21"}, {"value": "2026-05-22"}]},
            ),
            patch.object(api, "_generate_report_content_for_user") as generate,
            patch.object(api, "_build_report_info") as build_report,
            patch.object(api, "apply_execution_results_to_user") as apply_results,
            patch.object(api.time, "sleep") as sleep,
            patch.dict(os.environ, {"REPORT_MAKEUP_BATCH_DELAY_SECONDS": "0.5"}),
        ):
            generate.side_effect = [
                {
                    "api_client": clients[0],
                    "config": Mock(),
                    "meta": api._get_report_meta("daily"),
                    "content": "report-1",
                    "config_data": {"runtime": "first"},
                },
                {
                    "api_client": clients[1],
                    "config": Mock(),
                    "meta": api._get_report_meta("daily"),
                    "content": "report-2",
                    "config_data": {"runtime": "second"},
                },
            ]
            build_report.side_effect = [
                {"title": "第1天日报", "reportTime": "2026-05-21 12:00:00"},
                {"title": "第2天日报", "reportTime": "2026-05-22 12:00:00"},
            ]

            result, config_data, target_periods = api._makeup_all_reports_for_user(user, "daily")

        self.assertEqual(result["status"], "success")
        self.assertEqual(target_periods, ["2026-05-21", "2026-05-22"])
        self.assertEqual(result["details"]["补交周期数"], 2)
        self.assertEqual(result["details"]["成功"], 2)
        self.assertEqual(result["details"]["失败"], 0)
        self.assertEqual(result["details"]["请求间隔秒"], 0.5)
        self.assertEqual(config_data, {"runtime": "second"})
        self.assertEqual(generate.call_count, 2)
        clients[0].submit_report.assert_called_once()
        clients[1].submit_report.assert_called_once()
        sleep.assert_called_once_with(0.5)
        apply_results.assert_called_once()

    def test_makeup_all_reports_keeps_report_type_separate(self):
        user = User(phone="tester", password="secret")

        for report_key in ("daily", "weekly", "monthly"):
            with self.subTest(report_key=report_key):
                api_client = Mock()
                with (
                    patch.object(
                        api,
                        "_get_missing_report_periods_for_user",
                        return_value={"options": [{"value": "2026-05-22"}]},
                    ) as missing,
                    patch.object(api, "_generate_report_content_for_user") as generate,
                    patch.object(api, "_build_report_info", return_value={"title": "report", "reportTime": "2026-05-22 12:00:00"}),
                    patch.object(api, "apply_execution_results_to_user"),
                    patch.object(api.time, "sleep"),
                    patch.dict(os.environ, {"REPORT_MAKEUP_BATCH_DELAY_SECONDS": "0"}),
                ):
                    generate.return_value = {
                        "api_client": api_client,
                        "config": Mock(),
                        "meta": api._get_report_meta(report_key),
                        "content": "report",
                        "config_data": {},
                    }

                    result, _, target_periods = api._makeup_all_reports_for_user(user, report_key)

                self.assertEqual(result["status"], "success")
                self.assertEqual(target_periods, ["2026-05-22"])
                missing.assert_called_once_with(user, report_key)
                generate.assert_called_once_with(user, report_key, "2026-05-22", generate_content=True)

    def test_build_weekly_report_ignores_same_weeks_label_when_period_differs(self):
        api_client = Mock()
        api_client.get_submitted_reports_info.return_value = {
            "flag": 2,
            "data": [
                {
                    "weeks": "\u7b2c3\u5468",
                    "startTime": "2026-05-18 00:00:00",
                    "endTime": "2026-05-24 23:59:59",
                }
            ],
        }
        api_client.get_job_info.return_value = {"jobId": "job-1"}
        api_client.get_from_info.return_value = []

        report_info = api._build_report_info(
            api_client=api_client,
            config=ConfigManager(config={}),
            meta=api._get_report_meta("weekly"),
            content="weekly content",
            target_period="2026-05-04",
        )

        self.assertEqual(report_info["startTime"], "2026-05-04 00:00:00")
        self.assertEqual(report_info["endTime"], "2026-05-10 23:59:59")

    def test_build_daily_report_ignores_same_title_when_date_differs(self):
        api_client = Mock()
        api_client.get_submitted_reports_info.return_value = {
            "flag": 2,
            "data": [
                {
                    "title": "\u7b2c3\u5929\u65e5\u62a5",
                    "reportTime": "2026-05-21 12:00:00",
                }
            ],
        }
        api_client.get_job_info.return_value = {"jobId": "job-1"}
        api_client.get_from_info.return_value = []

        report_info = api._build_report_info(
            api_client=api_client,
            config=ConfigManager(config={}),
            meta=api._get_report_meta("daily"),
            content="daily content",
            target_period="2026-05-22",
        )

        self.assertEqual(report_info["reportTime"], "2026-05-22 00:00:00")

    def test_build_daily_report_detects_same_day_from_date_only_report_time(self):
        api_client = Mock()
        api_client.get_submitted_reports_info.return_value = {
            "flag": 1,
            "data": [
                {
                    "reportTime": "2026-05-22",
                }
            ],
        }
        api_client.get_job_info.return_value = {"jobId": "job-1"}
        api_client.get_from_info.return_value = []

        with self.assertRaises(HTTPException) as cm:
            api._build_report_info(
                api_client=api_client,
                config=ConfigManager(config={}),
                meta=api._get_report_meta("daily"),
                content="daily content",
                target_period="2026-05-22",
            )

        self.assertEqual(cm.exception.status_code, 400)

    def test_build_monthly_report_detects_same_month_from_report_time_when_yearmonth_missing(self):
        api_client = Mock()
        api_client.get_submitted_reports_info.return_value = {
            "flag": 1,
            "data": [
                {
                    "reportTime": "2026-05-20 12:00:00",
                }
            ],
        }
        api_client.get_job_info.return_value = {"jobId": "job-1"}
        api_client.get_from_info.return_value = []

        with self.assertRaises(HTTPException) as cm:
            api._build_report_info(
                api_client=api_client,
                config=ConfigManager(config={}),
                meta=api._get_report_meta("monthly"),
                content="monthly content",
                target_period="2026-05",
            )

        self.assertEqual(cm.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
