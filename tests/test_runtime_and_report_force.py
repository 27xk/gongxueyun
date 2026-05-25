import datetime
import json
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from server.batch_jobs import count_batch_job_items_by_status, get_batch_job_item_status_counts
from server.clockin_backfill import build_missing_clockin_day_options
from server.coreApi.MainLogicApi import ApiClient, _clear_ip_restriction_state
from server.models import BatchJobItem
from server import task_runner
from server import api
from server.task_runner import _submit_report_common
from server.util.Config import ConfigManager
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

    def test_force_weekly_report_ignores_same_weeks_label_when_period_differs(self):
        config = Mock()
        config.get_value.side_effect = lambda key: {
            "config.reportSettings.weekly.enabled": True,
            "planInfo.planPaper.weekPaperNum": 1,
            "config.reportSettings.weekly.imageCount": 0,
            "userInfo.orgJson.snowFlakeId": "org-1",
            "userInfo.userId": "user-1",
        }.get(key)
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
        api_client.get_upload_token.return_value = "upload-token"
        api_client.get_from_info.return_value = []

        with (
            patch("server.task_runner.generate_article", return_value="weekly content"),
            patch("server.task_runner.upload_img", return_value=""),
        ):
            result = _submit_report_common(
                api_client=api_client,
                config=config,
                report_type="week",
                title_func=lambda count: f"week-{count}",
                check_time_func=lambda _: False,
                get_submitted_func=lambda: api_client.get_submitted_reports_info("week"),
                paper_num_key="planInfo.planPaper.weekPaperNum",
                image_count_key="config.reportSettings.weekly.imageCount",
                task_name="weekly report",
                form_type=8,
                force_report=True,
                target_period="2026-05-04",
            )

        self.assertEqual(result["status"], "success")
        api_client.submit_report.assert_called_once()
        submitted = api_client.submit_report.call_args.args[0]
        self.assertEqual(submitted["startTime"], "2026-05-04 00:00:00")
        self.assertEqual(submitted["endTime"], "2026-05-10 23:59:59")

    def test_force_daily_report_detects_same_day_from_date_only_report_time(self):
        config = Mock()
        config.get_value.side_effect = lambda key: {
            "config.reportSettings.daily.enabled": True,
            "planInfo.planPaper.dayPaperNum": 1,
            "config.reportSettings.daily.imageCount": 0,
            "userInfo.orgJson.snowFlakeId": "org-1",
            "userInfo.userId": "user-1",
        }.get(key)
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
        api_client.get_upload_token.return_value = "upload-token"
        api_client.get_from_info.return_value = []

        with (
            patch("server.task_runner.generate_article", return_value="daily content"),
            patch("server.task_runner.upload_img", return_value=""),
        ):
            result = _submit_report_common(
                api_client=api_client,
                config=config,
                report_type="day",
                title_func=lambda count: f"day-{count}",
                check_time_func=lambda _: False,
                get_submitted_func=lambda: api_client.get_submitted_reports_info("day"),
                paper_num_key="planInfo.planPaper.dayPaperNum",
                image_count_key="config.reportSettings.daily.imageCount",
                task_name="daily report",
                form_type=7,
                force_report=True,
                target_period="2026-05-22",
            )

        self.assertEqual(result["status"], "skip")
        api_client.submit_report.assert_not_called()

    def test_force_monthly_report_detects_same_month_from_report_time_when_yearmonth_missing(self):
        config = Mock()
        config.get_value.side_effect = lambda key: {
            "config.reportSettings.monthly.enabled": True,
            "planInfo.planPaper.monthPaperNum": 1,
            "config.reportSettings.monthly.imageCount": 0,
            "userInfo.orgJson.snowFlakeId": "org-1",
            "userInfo.userId": "user-1",
        }.get(key)
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
        api_client.get_upload_token.return_value = "upload-token"
        api_client.get_from_info.return_value = []

        with (
            patch("server.task_runner.generate_article", return_value="monthly content"),
            patch("server.task_runner.upload_img", return_value=""),
        ):
            result = _submit_report_common(
                api_client=api_client,
                config=config,
                report_type="month",
                title_func=lambda count: f"month-{count}",
                check_time_func=lambda _: False,
                get_submitted_func=lambda: api_client.get_submitted_reports_info("month"),
                paper_num_key="planInfo.planPaper.monthPaperNum",
                image_count_key="config.reportSettings.monthly.imageCount",
                task_name="monthly report",
                form_type=9,
                force_report=True,
                target_period="2026-05",
            )

        self.assertEqual(result["status"], "skip")
        api_client.submit_report.assert_not_called()

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


class FakeMogudingResponse:
    def __init__(self, payload, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class MapchaxunGeocodeProviderTest(unittest.TestCase):
    def setUp(self):
        with api._GEOCODE_LOCK:
            api._GEOCODE_CACHE.clear()

    def test_mapchaxun_geocode_search_uses_internal_api(self):
        payload = {
            "status": 10000,
            "message": "Success",
            "result": {
                "title": "仙鹅村",
                "location": {"lng": 111.990776, "lat": 27.130178},
                "address_components": {
                    "province": "湖南省",
                    "city": "邵阳市",
                    "district": "邵东市",
                    "street": "",
                    "street_number": "",
                },
            },
            "adress": "仙鹅村",
            "locationVal": "111.990776,27.130178",
            "location": [111.990776, 27.130178],
            "address_components": {
                "province": "湖南省",
                "city": "邵阳市",
                "district": "邵东市",
                "street": "",
                "adcode": "430582",
            },
            "elevation": 194,
        }
        with (
            patch.dict("os.environ", {"GEOCODE_SEARCH_PROVIDER": "mapchaxun"}, clear=False),
            patch("server.api.requests.post", return_value=FakeMogudingResponse(payload)) as post,
        ):
            result = api.geocode_search(q="湖南省邵阳市邵东市水东江镇仙鹅村")

        best = result["results"][0]
        self.assertEqual(best["x"], 111.990776)
        self.assertEqual(best["y"], 27.130178)
        self.assertEqual(best["label"], "仙鹅村")
        self.assertEqual(best["address"]["province"], "湖南省")
        self.assertEqual(best["address"]["city"], "邵阳市")
        self.assertEqual(best["address"]["district"], "邵东市")
        self.assertEqual(best["address"]["adcode"], "430582")
        call = post.call_args
        self.assertEqual(call.args[0], "https://www.mapchaxun.cn/api/getSolidAdress")
        self.assertEqual(call.kwargs["headers"]["content-type"], "application/json")
        self.assertEqual(call.kwargs["data"], json.dumps({"address": "湖南省邵阳市邵东市水东江镇仙鹅村"}, separators=(",", ":")))

    def test_mapchaxun_geocode_search_is_independent_from_global_provider(self):
        payload = {
            "status": 10000,
            "message": "Success",
            "result": {
                "title": "仙鹅村",
                "location": {"lng": 111.990776, "lat": 27.130178},
            },
            "adress": "仙鹅村",
        }
        with (
            patch.dict("os.environ", {"GEOCODE_PROVIDER": "osm", "GEOCODE_SEARCH_PROVIDER": ""}, clear=False),
            patch("server.api.requests.post", return_value=FakeMogudingResponse(payload)) as post,
            patch("server.api.requests.get", side_effect=AssertionError("search should use mapchaxun post")),
        ):
            result = api.geocode_search(q="湖南省邵阳市邵东市水东江镇仙鹅村")

        self.assertEqual(result["results"][0]["x"], 111.990776)
        post.assert_called_once()


class AuditLogMaintenanceTest(unittest.TestCase):
    def test_admin_can_clear_all_audit_logs(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                [
                    api.AuditLog(actor="admin", action="auth.login", target_user_id=None, detail={}),
                    api.AuditLog(actor="admin", action="user.update", target_user_id=1, detail={"fields": ["remark"]}),
                ]
            )
            session.commit()

            result = api.clear_audit_logs(session=session, admin={"sub": "admin", "role": "admin"})
            remaining = session.exec(select(api.AuditLog)).all()

        self.assertEqual(result, {"ok": True, "deleted": 2})
        self.assertEqual(remaining, [])


class ApiClientIPRestrictionTest(unittest.TestCase):
    def setUp(self):
        _clear_ip_restriction_state()

    def tearDown(self):
        _clear_ip_restriction_state()

    def test_ip_restricted_403_is_not_treated_as_token_expired(self):
        msg = "IP非法请求过多，已限制访问:111.23.44.229"

        self.assertFalse(ApiClient._is_token_expired_response(403, msg))

    def test_ip_restricted_response_blocks_later_unproxied_requests(self):
        msg = "IP非法请求过多，已限制访问:111.23.44.229"
        with patch("server.coreApi.MainLogicApi.load_global_proxy_settings", return_value={}):
            client = ApiClient(ConfigManager(config={}))
        client.session.post = Mock(return_value=FakeMogudingResponse({"code": 403, "msg": msg}))

        with self.assertRaises(ValueError) as ctx:
            client._post_request("test/path", {}, {})
        with self.assertRaises(ValueError):
            client._post_request("test/path", {}, {})

        self.assertIn("IP非法请求过多", str(ctx.exception))
        self.assertEqual(client.session.post.call_count, 1)

        with patch("server.coreApi.MainLogicApi.load_global_proxy_settings", return_value={}):
            second_client = ApiClient(ConfigManager(config={}))
        second_client.session.post = Mock()
        with self.assertRaises(ValueError):
            second_client._post_request("test/path", {}, {})
        second_client.session.post.assert_not_called()

    def test_ip_restricted_response_rotates_proxy_for_makeup_retry(self):
        msg = "IP非法请求过多，已限制访问:111.23.44.229"
        proxy_api = "http://proxy.example/get?accessName=u&accessPassword=p"
        with patch.dict("os.environ", {"MOGUDING_PROXY_API_URL": proxy_api}, clear=False):
            with patch(
                "server.coreApi.MainLogicApi.requests.get",
                side_effect=[
                    FakeMogudingResponse({}, text="1.2.3.4:5678\n"),
                    FakeMogudingResponse({}, text="5.6.7.8:9012\n"),
                ],
            ):
                client = ApiClient(ConfigManager(config={}))
                client.enable_proxy()
                client.max_retries = 2
                client.session.post = Mock(
                    side_effect=[
                        FakeMogudingResponse({"code": 403, "msg": msg}),
                        FakeMogudingResponse({"code": 200, "msg": "ok"}),
                    ]
                )
                with patch("server.coreApi.MainLogicApi.time.sleep"):
                    result = client._post_request("test/path", {}, {})

        self.assertEqual(result["code"], 200)
        calls = client.session.post.call_args_list
        self.assertEqual(calls[0].kwargs["proxies"]["https"], "http://u:p@1.2.3.4:5678")
        self.assertEqual(calls[1].kwargs["proxies"]["https"], "http://u:p@5.6.7.8:9012")


class TaskRunnerIPRestrictionTest(unittest.TestCase):
    def test_run_task_stops_remaining_moguding_tasks_after_ip_restriction(self):
        config_data = {
            "config": {
                "pushNotifications": [],
                "clockIn": {"schedule": {"startTime": "07:30", "endTime": "18:00"}},
                "reportSettings": {
                    "daily": {"enabled": True},
                    "weekly": {"enabled": True},
                    "monthly": {"enabled": True},
                },
            },
            "userInfo": {"token": "token", "expiredTime": "1893456000", "userType": "student", "nikeName": "tester"},
            "planInfo": {"planId": "plan-1"},
        }
        limited = {"status": "fail", "message": "打卡失败: IP非法请求过多，已限制访问:111.23.44.229", "task_type": "打卡"}

        with (
            patch.object(task_runner, "ApiClient"),
            patch.object(task_runner, "MessagePusher") as pusher_cls,
            patch.object(task_runner, "_load_global_smtp_settings", return_value={}),
            patch.object(task_runner, "perform_clock_in", return_value=limited) as clock_in,
            patch.object(task_runner, "submit_daily_report") as daily,
            patch.object(task_runner, "submit_weekly_report") as weekly,
            patch.object(task_runner, "submit_monthly_report") as monthly,
        ):
            pusher_cls.return_value.push = Mock()
            results = task_runner.run_task_by_config(config_data, forced_checkin_type="START")

        self.assertEqual(results, [limited])
        clock_in.assert_called_once()
        daily.assert_not_called()
        weekly.assert_not_called()
        monthly.assert_not_called()


class BaiduGeocodeProviderTest(unittest.TestCase):
    def setUp(self):
        with api._GEOCODE_LOCK:
            api._GEOCODE_CACHE.clear()

    def test_baidu_geocode_search_uses_baidu_web_api(self):
        payload = {
            "status": 0,
            "result": {
                "location": {"lng": 104.0668, "lat": 30.5728},
                "level": "地产小区",
                "confidence": 80,
                "comprehension": 100,
            },
        }
        with (
            patch.dict(
                "os.environ",
                {
                    "GEOCODE_SEARCH_PROVIDER": "baidu",
                    "BAIDU_MAP_AK": "ak-test",
                    "BAIDU_MAP_COORD_TYPE": "gcj02ll",
                    "BAIDU_MAP_INPUT_COORD_TYPE": "gcj02ll",
                    "BAIDU_MAP_OUTPUT_COORD_TYPE": "gcj02ll",
                },
                clear=False,
            ),
            patch("server.api.requests.get", return_value=FakeMogudingResponse(payload)) as get,
        ):
            result = api.geocode_search(q="成都市高新区天府大道")

        self.assertEqual(result["results"][0]["x"], 104.0668)
        self.assertEqual(result["results"][0]["y"], 30.5728)
        call = get.call_args
        self.assertEqual(call.args[0], "https://api.map.baidu.com/geocoding/v3/")
        self.assertEqual(call.kwargs["params"]["ak"], "ak-test")
        self.assertEqual(call.kwargs["params"]["address"], "成都市高新区天府大道")
        self.assertEqual(call.kwargs["params"]["ret_coordtype"], "gcj02ll")

    def test_baidu_reverse_geocode_uses_baidu_web_api(self):
        payload = {
            "status": 0,
            "result": {
                "formatted_address": "四川省成都市武侯区天府大道",
                "addressComponent": {
                    "province": "四川省",
                    "city": "成都市",
                    "district": "武侯区",
                    "town": "桂溪街道",
                    "street": "天府大道",
                    "street_number": "1号",
                },
            },
        }
        with (
            patch.dict(
                "os.environ",
                {
                    "GEOCODE_PROVIDER": "baidu",
                    "BAIDU_MAP_AK": "ak-test",
                    "BAIDU_MAP_COORD_TYPE": "gcj02ll",
                    "BAIDU_MAP_INPUT_COORD_TYPE": "gcj02ll",
                    "BAIDU_MAP_OUTPUT_COORD_TYPE": "bd09ll",
                },
                clear=False,
            ),
            patch("server.api.requests.get", return_value=FakeMogudingResponse(payload)) as get,
        ):
            result = api.geocode_reverse(lat=30.5728, lon=104.0668)

        address = result["result"]["address"]
        self.assertEqual(result["result"]["display_name"], "四川省成都市武侯区天府大道")
        self.assertEqual(address["province"], "四川省")
        self.assertEqual(address["city"], "成都市")
        self.assertEqual(address["district"], "武侯区")
        call = get.call_args
        self.assertEqual(call.args[0], "https://api.map.baidu.com/reverse_geocoding/v3/")
        self.assertEqual(call.kwargs["params"]["ak"], "ak-test")
        self.assertEqual(call.kwargs["params"]["location"], "30.5728,104.0668")
        self.assertEqual(call.kwargs["params"]["coordtype"], "gcj02ll")
        self.assertEqual(call.kwargs["params"]["ret_coordtype"], "bd09ll")

    def test_baidu_provider_without_key_returns_clear_error(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "GEOCODE_SEARCH_PROVIDER": "baidu",
                    "BAIDU_MAP_AK": "",
                    "BAIDU_MAP_KEY": "",
                    "BAIDU_MAP_INPUT_COORD_TYPE": "",
                    "BAIDU_MAP_OUTPUT_COORD_TYPE": "",
                },
                clear=False,
            ),
            patch("server.api.requests.get") as get,
        ):
            with self.assertRaises(HTTPException) as ctx:
                api.geocode_search(q="成都市高新区天府大道")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("BAIDU_MAP_AK", str(ctx.exception.detail))
        get.assert_not_called()

    def test_baidu_geocode_cache_separates_output_coord_type(self):
        payload_gcj = {"status": 0, "result": {"location": {"lng": 104.1, "lat": 30.1}}}
        payload_bd = {"status": 0, "result": {"location": {"lng": 104.2, "lat": 30.2}}}

        with patch("server.api.requests.get", side_effect=[FakeMogudingResponse(payload_gcj), FakeMogudingResponse(payload_bd)]) as get:
            with patch.dict(
                "os.environ",
                {
                    "GEOCODE_SEARCH_PROVIDER": "baidu",
                    "BAIDU_MAP_AK": "ak-test",
                    "BAIDU_MAP_OUTPUT_COORD_TYPE": "gcj02ll",
                },
                clear=False,
            ):
                first = api.geocode_search(q="成都市高新区天府大道")
            with patch.dict(
                "os.environ",
                {
                    "GEOCODE_SEARCH_PROVIDER": "baidu",
                    "BAIDU_MAP_AK": "ak-test",
                    "BAIDU_MAP_OUTPUT_COORD_TYPE": "bd09ll",
                },
                clear=False,
            ):
                second = api.geocode_search(q="成都市高新区天府大道")

        self.assertEqual(first["results"][0]["x"], 104.1)
        self.assertEqual(second["results"][0]["x"], 104.2)
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
