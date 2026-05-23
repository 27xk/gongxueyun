import datetime
import unittest
from unittest.mock import Mock

from sqlmodel import Session, SQLModel, create_engine

from server.batch_jobs import count_batch_job_items_by_status, get_batch_job_item_status_counts
from server.clockin_backfill import build_missing_clockin_day_options
from server.models import BatchJobItem
from server.task_runner import _submit_report_common
from server.user_runtime import runtime_login_valid


class RuntimeAndManualReportTest(unittest.TestCase):
    def test_runtime_login_valid_accepts_second_precision_expired_time(self):
        self.assertTrue(runtime_login_valid({"token": "abc", "expiredTime": "1893456000"}, now_ms=1760000000000))

    def test_force_report_ignores_disabled_report_switch(self):
        config = Mock()
        config.get_value.side_effect = lambda key: False if key == "config.reportSettings.daily.enabled" else 0
        api_client = Mock()
        api_client.get_job_info.return_value = {"jobId": "job-1"}
        api_client.get_from_info.return_value = []

        result = _submit_report_common(
            api_client=api_client,
            config=config,
            report_type="day",
            title_func=lambda count: f"day-{count}",
            check_time_func=lambda _: False,
            get_submitted_func=lambda: {"flag": 0, "data": []},
            paper_num_key="planInfo.planPaper.dayPaperNum",
            image_count_key="config.reportSettings.daily.imageCount",
            task_name="日报提交",
            form_type=7,
            force_report=True,
            target_period="2026-05-22",
        )

        self.assertNotEqual(result["status"], "skip")

    def test_missing_clockin_can_ignore_scheduled_weekdays_for_manual_makeup(self):
        options = build_missing_clockin_day_options(
            records=[],
            start_date="2026-05-18",
            end_date="2026-05-19",
            scheduled_weekdays=[1],
            respect_scheduled_weekdays=False,
        )

        self.assertEqual([item["value"] for item in options], ["2026-05-19", "2026-05-18"])


class BatchJobQueryTest(unittest.TestCase):
    def test_status_counts_are_grouped_by_database(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                [
                    BatchJobItem(job_id=1, user_id=101, status="queued"),
                    BatchJobItem(job_id=1, user_id=102, status="queued"),
                    BatchJobItem(job_id=1, user_id=103, status="running"),
                    BatchJobItem(job_id=2, user_id=201, status="queued"),
                ]
            )
            session.commit()

            counts = get_batch_job_item_status_counts(session, 1, ["queued", "running"])
            running = count_batch_job_items_by_status(session, 1, "running")

        self.assertEqual(counts, {"queued": 2, "running": 1})
        self.assertEqual(running, 1)


class ModelDownloadConfigTest(unittest.TestCase):
    def test_default_model_urls_point_to_project_repo(self):
        from server.util import CaptchaUtils

        base_url = "https://raw.githubusercontent.com/27xk/gongxueyun/main/server/models_onnx"
        self.assertEqual(CaptchaUtils.MODEL_BASE_URL, base_url)
        self.assertEqual(CaptchaUtils.MODEL_URLS["ocr.onnx"], f"{base_url}/ocr.onnx")
        self.assertEqual(CaptchaUtils.MODEL_URLS["yolov5n.onnx"], f"{base_url}/yolov5n.onnx")


if __name__ == "__main__":
    unittest.main()
