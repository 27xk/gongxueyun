import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from server import api, task_runner
from server.coreApi.MainLogicApi import ApiClient
from server.models import AuditLog, User
from server.user_runtime import apply_execution_results_to_user
from server.util.MessagePush import MessagePusher


SAFE_REGISTRATION = {
    "outRegisterNo": "test-123-AbCd",
    "registerUrl": "alipays://platformapi/startapp?appId=test",
}


class FakeConfig:
    def __init__(self, values=None):
        self.values = values or {}

    def get_value(self, key):
        return self.values.get(key)


def build_api_client():
    client = ApiClient.__new__(ApiClient)
    client.config = FakeConfig(
        {
            "config.device": "test-device",
            "config.clockIn.location": {
                "address": "test address",
                "latitude": "30.1",
                "longitude": "120.1",
            },
            "config.clockIn.location.address": "test address",
            "planInfo.planId": "test-plan",
            "userInfo.roleKey": "student",
            "userInfo.token": "test-token",
            "userInfo.userId": "test-user",
            "userInfo.userType": "student",
        }
    )
    client.max_retries = 1
    return client


def build_checkin_info():
    return {
        "type": "START",
        "attachments": None,
        "description": "test description",
        "createTime": "2026-07-15 08:00:00",
        "attendenceTime": "2026-07-15 08:00:00",
    }


def build_task_config():
    return FakeConfig(
        {
            "config.clockIn.customDays": [],
            "config.clockIn.description": [],
            "config.clockIn.imageCount": 0,
            "config.clockIn.location.address": "test address",
            "config.clockIn.mode": "none",
            "config.clockIn.specialClockIn": False,
            "userInfo.nikeName": "Test User",
            "userInfo.orgJson.snowFlakeId": "test-org",
            "userInfo.userId": "test-user",
        }
    )


def build_task_api_client(submit_result, *, replace=False):
    client = Mock()
    client.get_checkin_records.return_value = []
    client.get_upload_token.return_value = "test-upload-token"
    client.create_alipay_clockin_verification.return_value = dict(SAFE_REGISTRATION)
    if replace:
        client.submit_clock_in_replace.return_value = submit_result
    else:
        client.submit_clock_in.return_value = submit_result
    return client


class AlipayApiClientTest(unittest.TestCase):
    def test_post_request_returns_clockin_business_responses(self):
        client = build_api_client()
        response = Mock(status_code=200, text="")
        response.raise_for_status.return_value = None

        for msg in ("302", "304"):
            with self.subTest(msg=msg):
                response.json.return_value = {"code": 200, "msg": msg, "data": "test"}
                client.session = Mock()
                client.session.post.return_value = response
                with patch.object(client, "_proxy_request_kwargs", return_value={}):
                    result = client._post_request("test/path", {}, {})
                self.assertEqual(result["msg"], msg)

    def test_create_alipay_verification_extracts_safe_registration(self):
        client = build_api_client()
        client._post_request = Mock(
            return_value={"code": 200, "msg": "success", "data": dict(SAFE_REGISTRATION)}
        )

        result = client.create_alipay_clockin_verification()

        self.assertEqual(result, SAFE_REGISTRATION)
        path, headers, payload = client._post_request.call_args.args
        self.assertEqual(path, "usercenter/alipay/v1/createAxdjk")
        self.assertEqual(headers["authorization"], "test-token")
        self.assertEqual(set(payload), {"t"})
        self.assertIsInstance(payload["t"], str)
        self.assertTrue(payload["t"])

    def test_create_alipay_verification_rejects_non_alipay_scheme(self):
        client = build_api_client()
        unsafe_url = "https://example.test/verify?secret=hidden"
        client._post_request = Mock(
            return_value={
                "code": 200,
                "msg": "success",
                "data": {
                    "outRegisterNo": SAFE_REGISTRATION["outRegisterNo"],
                    "registerUrl": unsafe_url,
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "支付宝安全验证链接无效") as ctx:
            client.create_alipay_clockin_verification()

        self.assertNotIn(unsafe_url, str(ctx.exception))

    def test_create_alipay_verification_rejects_non_hierarchical_alipay_url(self):
        client = build_api_client()
        client._post_request = Mock(
            return_value={
                "code": 200,
                "msg": "success",
                "data": {
                    "outRegisterNo": SAFE_REGISTRATION["outRegisterNo"],
                    "registerUrl": "alipays:platformapi/startapp?appId=test",
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "支付宝安全验证链接无效"):
            client.create_alipay_clockin_verification()

    def test_create_alipay_verification_rejects_incomplete_response(self):
        client = build_api_client()
        client._post_request = Mock(
            return_value={
                "code": 200,
                "msg": "success",
                "data": {"outRegisterNo": SAFE_REGISTRATION["outRegisterNo"]},
            }
        )

        with self.assertRaisesRegex(ValueError, "支付宝安全验证响应不完整"):
            client.create_alipay_clockin_verification()

    def test_initial_304_reports_verification_without_creating_registration(self):
        client = build_api_client()
        client._post_request = Mock(return_value={"code": 200, "msg": "304", "data": "verify"})
        client.create_alipay_clockin_verification = Mock(return_value=dict(SAFE_REGISTRATION))

        result = client.submit_clock_in(build_checkin_info())

        self.assertEqual(result, {"status": "verification_required"})
        client._post_request.assert_called_once()
        client.create_alipay_clockin_verification.assert_not_called()
        submitted_payload = client._post_request.call_args.args[2]
        self.assertIsNone(submitted_payload["outRegisterNo"])

    def test_continue_304_reports_verification_without_retrying_clockin(self):
        client = build_api_client()
        client._post_request = Mock(return_value={"code": 200, "msg": "304", "data": "verify"})
        refreshed_registration = {
            "outRegisterNo": "test-456-EfGh",
            "registerUrl": "alipays://platformapi/startapp?appId=refreshed",
        }
        client.create_alipay_clockin_verification = Mock(
            return_value=refreshed_registration
        )
        checkin_info = build_checkin_info()
        checkin_info["outRegisterNo"] = SAFE_REGISTRATION["outRegisterNo"]

        result = client.submit_clock_in(checkin_info)

        self.assertEqual(result, {"status": "verification_required"})
        client._post_request.assert_called_once()
        self.assertEqual(
            client._post_request.call_args.args[2]["outRegisterNo"],
            SAFE_REGISTRATION["outRegisterNo"],
        )
        client.create_alipay_clockin_verification.assert_not_called()

    def test_behavior_captcha_still_retries_clockin_once(self):
        client = build_api_client()
        client._post_request = Mock(
            side_effect=[
                {"code": 200, "msg": "302", "data": "captcha"},
                {"code": 200, "msg": "success", "data": "ok"},
            ]
        )
        client.solve_click_word_captcha = Mock(return_value="test-captcha")

        result = client.submit_clock_in(build_checkin_info())

        self.assertEqual(result, {"status": "success"})
        self.assertEqual(client._post_request.call_count, 2)
        self.assertEqual(
            client._post_request.call_args_list[1].args[2]["captcha"],
            "test-captcha",
        )
        client.solve_click_word_captcha.assert_called_once_with()

    def test_continue_clockin_submits_out_register_number_once(self):
        client = build_api_client()
        client._post_request = Mock(return_value={"code": 200, "msg": "success", "data": "ok"})
        checkin_info = build_checkin_info()
        checkin_info["outRegisterNo"] = SAFE_REGISTRATION["outRegisterNo"]

        result = client.submit_clock_in(checkin_info)

        self.assertEqual(result, {"status": "success"})
        client._post_request.assert_called_once()
        submitted_payload = client._post_request.call_args.args[2]
        self.assertEqual(submitted_payload["outRegisterNo"], SAFE_REGISTRATION["outRegisterNo"])


class ClockInTaskResultTest(unittest.TestCase):
    def test_perform_clockin_maps_verification_to_fail_details(self):
        api_client = build_task_api_client(
            {"status": "verification_required", **SAFE_REGISTRATION}
        )

        with patch.object(task_runner, "upload_img", return_value=[]):
            result = task_runner.perform_clock_in(
                api_client,
                build_task_config(),
                forced_checkin_type="START",
            )

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["task_type"], "打卡")
        self.assertIn("支付宝安全验证", result["message"])
        self.assertEqual(result["details"]["target_type"], "START")
        self.assertEqual(
            result["details"]["outRegisterNo"],
            SAFE_REGISTRATION["outRegisterNo"],
        )
        self.assertEqual(result["details"]["registerUrl"], SAFE_REGISTRATION["registerUrl"])
        api_client.create_alipay_clockin_verification.assert_called_once_with()

    def test_replace_verification_is_not_reported_as_success_or_continuable(self):
        api_client = build_task_api_client(
            {"status": "verification_required", **SAFE_REGISTRATION},
            replace=True,
        )

        with patch.object(task_runner, "upload_img", return_value=[]):
            result = task_runner.perform_clock_in(
                api_client,
                build_task_config(),
                forced_checkin_type="START",
                replace=True,
            )

        self.assertEqual(result["status"], "fail")
        self.assertNotIn("outRegisterNo", result["details"])
        self.assertNotIn("registerUrl", result["details"])

    def test_continue_clockin_passes_registration_to_new_payload(self):
        api_client = build_task_api_client({"status": "success"})

        with patch.object(task_runner, "upload_img", return_value=[]):
            result = task_runner.perform_clock_in(
                api_client,
                build_task_config(),
                forced_checkin_type="END",
                out_register_no=SAFE_REGISTRATION["outRegisterNo"],
            )

        self.assertEqual(result["status"], "success")
        api_client.submit_clock_in.assert_called_once()
        submitted_checkin_info = api_client.submit_clock_in.call_args.args[0]
        self.assertEqual(
            submitted_checkin_info["outRegisterNo"],
            SAFE_REGISTRATION["outRegisterNo"],
        )

    def test_preauthorized_clockin_sends_token_only_after_initial_304(self):
        api_client = build_task_api_client({"status": "verification_required"})
        api_client.submit_clock_in.side_effect = [
            {"status": "verification_required"},
            {"status": "success"},
        ]
        hooks = Mock()
        hooks.claim.return_value = SimpleNamespace(id=17, out_register_no="stored-register-1")

        with patch.object(task_runner, "upload_img", return_value=[]):
            result = task_runner.perform_clock_in(
                api_client,
                build_task_config(),
                forced_checkin_type="START",
                target_time=task_runner.datetime(2026, 7, 16, 8, 30),
                preauthorization_hooks=hooks,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(api_client.submit_clock_in.call_count, 2)
        first_payload = api_client.submit_clock_in.call_args_list[0].args[0]
        second_payload = api_client.submit_clock_in.call_args_list[1].args[0]
        self.assertNotIn("outRegisterNo", first_payload)
        self.assertEqual(second_payload["outRegisterNo"], "stored-register-1")
        hooks.claim.assert_called_once_with(
            target_date=task_runner.datetime(2026, 7, 16).date(),
            target_type="START",
            used_target_type=None,
        )
        hooks.require_reauthorization.assert_not_called()
        api_client.create_alipay_clockin_verification.assert_not_called()

    def test_preauthorized_second_304_marks_reauthorization_and_stops(self):
        api_client = build_task_api_client({"status": "verification_required"})
        api_client.submit_clock_in.side_effect = [
            {"status": "verification_required"},
            {"status": "verification_required"},
        ]
        hooks = Mock()
        hooks.claim.return_value = SimpleNamespace(id=18, out_register_no="stored-register-2")

        with patch.object(task_runner, "upload_img", return_value=[]):
            result = task_runner.perform_clock_in(
                api_client,
                build_task_config(),
                forced_checkin_type="END",
                target_time=task_runner.datetime(2026, 7, 16, 18, 30),
                preauthorization_hooks=hooks,
            )

        self.assertEqual(result["status"], "fail")
        self.assertEqual(api_client.submit_clock_in.call_count, 2)
        hooks.require_reauthorization.assert_called_once_with(18)
        api_client.create_alipay_clockin_verification.assert_called_once_with()
        self.assertEqual(result["details"]["outRegisterNo"], SAFE_REGISTRATION["outRegisterNo"])

    def test_makeup_uses_shared_day_authorization_with_actual_type(self):
        api_client = build_task_api_client(
            {"status": "verification_required"},
            replace=True,
        )
        api_client.submit_clock_in_replace.side_effect = [
            {"status": "verification_required"},
            {"status": "success"},
        ]
        hooks = Mock()
        hooks.claim.return_value = SimpleNamespace(id=19, out_register_no="makeup-register-1")

        with patch.object(task_runner, "upload_img", return_value=[]):
            result = task_runner.perform_clock_in(
                api_client,
                build_task_config(),
                forced_checkin_type="END",
                target_time=task_runner.datetime(2026, 7, 14, 18, 30),
                replace=True,
                preauthorization_hooks=hooks,
            )

        self.assertEqual(result["status"], "success")
        hooks.claim.assert_called_once_with(
            target_date=task_runner.datetime(2026, 7, 14).date(),
            target_type="MAKEUP",
            used_target_type="END",
        )
        second_payload = api_client.submit_clock_in_replace.call_args_list[1].args[0]
        self.assertEqual(second_payload["outRegisterNo"], "makeup-register-1")
        api_client.create_alipay_clockin_verification.assert_not_called()

    def test_explicit_continue_does_not_claim_preauthorization(self):
        api_client = build_task_api_client({"status": "success"})
        hooks = Mock()

        with patch.object(task_runner, "upload_img", return_value=[]):
            result = task_runner.perform_clock_in(
                api_client,
                build_task_config(),
                forced_checkin_type="START",
                out_register_no=SAFE_REGISTRATION["outRegisterNo"],
                preauthorization_hooks=hooks,
            )

        self.assertEqual(result["status"], "success")
        hooks.claim.assert_not_called()


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


class AlipayContinueApiTest(unittest.TestCase):
    def test_admin_run_idempotency_record_does_not_persist_verification_details(self):
        user = User(
            id=10,
            tenant_id="acme",
            phone="test-user",
            password="test-password",
        )
        pending_result = {
            "status": "fail",
            "message": "需要完成支付宝安全验证",
            "task_type": "打卡",
            "details": {"target_type": "START", **SAFE_REGISTRATION},
        }
        request = SimpleNamespace(
            headers={"Idempotency-Key": "test-alipay-verification"},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        session = Mock()

        with (
            patch.object(api, "_manual_operation_existing_response", return_value=None),
            patch.object(api, "claim_idempotency_record", return_value=None),
            patch.object(api, "_rate_limit"),
            patch.object(api, "_get_active_user_for_payload", return_value=user),
            patch.object(api, "user_to_config", return_value={"config": {}}),
            patch.object(api, "run_task_by_config", return_value=[pending_result]),
            patch.object(api, "apply_execution_results_to_user", return_value="Fail"),
            patch.object(api, "finalize_idempotency_record") as finalize,
        ):
            response = api.run_user_task(
                request=request,
                session=session,
                user_id=10,
                req=api.AppRunRequest(task_type="clock_in"),
                operator={"sub": "operator", "tenant_id": "acme"},
            )

        self.assertEqual(
            response["results"][0]["details"]["outRegisterNo"],
            SAFE_REGISTRATION["outRegisterNo"],
        )
        stored_response = finalize.call_args.kwargs["response"]
        self.assertEqual(
            stored_response["results"][0]["details"],
            {"target_type": "START"},
        )

    def test_verification_details_are_not_in_message_push_content(self):
        pending_result = {
            "status": "fail",
            "message": "需要完成支付宝安全验证",
            "task_type": "打卡",
            "details": {
                "target_type": "START",
                **SAFE_REGISTRATION,
            },
        }

        markdown = MessagePusher._generate_markdown_message([pending_result])
        html = MessagePusher._generate_html_message([pending_result])

        for content in (markdown, html):
            self.assertNotIn(SAFE_REGISTRATION["outRegisterNo"], content)
            self.assertNotIn(SAFE_REGISTRATION["registerUrl"], content)
            self.assertIn("target_type", content)

    def test_verification_details_are_not_persisted(self):
        user = User(phone="test-user", password="test-password")
        pending_result = {
            "status": "fail",
            "message": "需要完成支付宝安全验证，请验证后继续打卡",
            "task_type": "打卡",
            "details": {
                "target_type": "START",
                **SAFE_REGISTRATION,
            },
        }

        apply_execution_results_to_user(
            user,
            [pending_result],
            {"config": {}, "userInfo": {}, "planInfo": {}},
        )

        stored_details = user.last_execution_result[0]["details"]
        self.assertEqual(stored_details, {"target_type": "START"})
        self.assertEqual(pending_result["details"]["outRegisterNo"], SAFE_REGISTRATION["outRegisterNo"])
        self.assertEqual(pending_result["details"]["registerUrl"], SAFE_REGISTRATION["registerUrl"])

    def test_continue_request_validates_registration_number_and_target_type(self):
        valid = api.AlipayClockInContinueRequest(
            out_register_no=SAFE_REGISTRATION["outRegisterNo"],
            target_type="end",
        )
        self.assertEqual(
            api._alipay_continue_values(valid),
            (SAFE_REGISTRATION["outRegisterNo"], "END"),
        )

        invalid_values = ["", "bad value", "bad/value", "x" * 129]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as ctx:
                    api._alipay_continue_values(
                        api.AlipayClockInContinueRequest(
                            out_register_no=value,
                            target_type="START",
                        )
                    )
                self.assertEqual(ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException) as ctx:
            api._alipay_continue_values(
                api.AlipayClockInContinueRequest(
                    out_register_no=SAFE_REGISTRATION["outRegisterNo"],
                    target_type="HOLIDAY",
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_continue_business_passes_registration_to_clockin_once(self):
        user = User(id=7, phone="test-user", password="test-password")
        success_result = {
            "status": "success",
            "message": "上班打卡成功",
            "task_type": "打卡",
            "details": {},
        }
        config_data = {"config": {}, "userInfo": {}, "planInfo": {}}

        with (
            patch.object(api, "user_to_config", return_value=config_data),
            patch.object(api, "ApiClient") as api_client_type,
            patch.object(api, "_ensure_remote_runtime") as ensure_runtime,
            patch.object(api, "perform_clock_in", return_value=success_result) as perform,
            patch.object(api, "apply_execution_results_to_user") as apply_results,
        ):
            result, returned_config = api._continue_alipay_clockin_for_user(
                user,
                SAFE_REGISTRATION["outRegisterNo"],
                "START",
            )

        self.assertEqual(result, success_result)
        self.assertIs(returned_config, config_data)
        ensure_runtime.assert_called_once()
        perform.assert_called_once()
        self.assertEqual(perform.call_args.kwargs["forced_checkin_type"], "START")
        self.assertEqual(
            perform.call_args.kwargs["out_register_no"],
            SAFE_REGISTRATION["outRegisterNo"],
        )
        apply_results.assert_called_once_with(user, [success_result], config_data)
        self.assertIs(ensure_runtime.call_args.args[0], api_client_type.return_value)

    def test_continue_routes_are_registered_with_admin_permission(self):
        routes = {getattr(route, "path", ""): route for route in api.router.routes}
        self.assertIn("/app/clock-in/alipay/continue", routes)
        admin_route = routes["/users/{user_id}/clock-in/alipay/continue"]
        dependency_calls = {
            dependency.call for dependency in admin_route.dependant.dependencies
        }
        self.assertIn(api.require_tasks_run, dependency_calls)

    def test_app_continue_uses_bound_user_limit_and_sanitized_audit(self):
        user = User(
            id=8,
            tenant_id="acme",
            phone="test-user",
            password="test-password",
        )
        request_model = api.AlipayClockInContinueRequest(
            out_register_no=SAFE_REGISTRATION["outRegisterNo"],
            target_type="START",
        )
        session = Mock()
        pending_result = {
            "status": "fail",
            "message": "需要完成支付宝安全验证，请验证后继续打卡",
            "task_type": "打卡",
            "details": dict(SAFE_REGISTRATION),
        }

        with (
            patch.object(api, "_get_authed_app_user", return_value=Mock()),
            patch.object(api, "_get_bound_task_user", return_value=user),
            patch.object(api, "_rate_limit") as rate_limit,
            patch.object(
                api,
                "_continue_alipay_clockin_for_user",
                return_value=(pending_result, {"config": {}}),
            ) as continue_clockin,
        ):
            response = api.app_continue_alipay_clockin(
                request=FakeRequest(),
                req=request_model,
                session=session,
                payload={"sub": "app:1", "tenant_id": "acme"},
            )

        self.assertEqual(response, {"result": pending_result})
        rate_limit.assert_called_once_with(
            "app_run:127.0.0.1:8",
            limit=3,
            per_seconds=60,
        )
        continue_clockin.assert_called_once_with(
            user,
            SAFE_REGISTRATION["outRegisterNo"],
            "START",
        )
        audit = next(
            call.args[0]
            for call in session.add.call_args_list
            if isinstance(call.args[0], AuditLog)
        )
        self.assertEqual(audit.detail, {"status": "fail", "target_type": "START"})
        self.assertNotIn(SAFE_REGISTRATION["outRegisterNo"], str(audit.detail))
        self.assertNotIn(SAFE_REGISTRATION["registerUrl"], str(audit.detail))
        session.commit.assert_called_once_with()

    def test_admin_continue_uses_tenant_scope_limit_and_sanitized_audit(self):
        user = User(
            id=9,
            tenant_id="acme",
            phone="test-user",
            password="test-password",
        )
        request_model = api.AlipayClockInContinueRequest(
            out_register_no=SAFE_REGISTRATION["outRegisterNo"],
            target_type="END",
        )
        session = Mock()
        success_result = {
            "status": "success",
            "message": "下班打卡成功",
            "task_type": "打卡",
            "details": {},
        }
        operator = {"sub": "operator", "tenant_id": "acme"}

        with (
            patch.object(api, "_get_active_user_for_payload", return_value=user) as get_user,
            patch.object(api, "_rate_limit") as rate_limit,
            patch.object(
                api,
                "_continue_alipay_clockin_for_user",
                return_value=(success_result, {"config": {}}),
            ),
        ):
            response = api.continue_user_alipay_clockin(
                request=FakeRequest(),
                req=request_model,
                session=session,
                user_id=9,
                operator=operator,
            )

        self.assertEqual(response, {"result": success_result})
        get_user.assert_called_once_with(session, 9, operator)
        rate_limit.assert_called_once_with(
            "run:127.0.0.1:9",
            limit=2,
            per_seconds=60,
        )
        audit = next(
            call.args[0]
            for call in session.add.call_args_list
            if isinstance(call.args[0], AuditLog)
        )
        self.assertEqual(audit.detail, {"status": "success", "target_type": "END"})
        self.assertNotIn(SAFE_REGISTRATION["outRegisterNo"], str(audit.detail))
        session.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
