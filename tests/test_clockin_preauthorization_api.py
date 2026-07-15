import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server import api
from server.models import ClockInPreauthorization, User


def build_user():
    return User(
        id=7,
        tenant_id="tenant-a",
        phone="13800000000",
        password="encrypted",
        created_at=datetime.datetime(2026, 7, 13, tzinfo=datetime.timezone.utc),
        planInfo={"endTime": "2026-07-20 23:59:59"},
        clockIn={
            "schedule": {
                "weekdays": [1, 2, 3, 4, 5],
                "startTime": "08:30",
                "endTime": "18:30",
            }
        },
    )


class ClockInPreauthorizationApiTest(unittest.TestCase):
    def test_routes_are_registered_for_both_surfaces(self):
        routes = {getattr(route, "path", ""): route for route in api.router.routes}
        expected = {
            "/app/clock-in/preauthorizations",
            "/app/clock-in/preauthorizations/start",
            "/app/clock-in/preauthorizations/complete",
            "/users/{user_id}/clock-in/preauthorizations",
            "/users/{user_id}/clock-in/preauthorizations/start",
            "/users/{user_id}/clock-in/preauthorizations/complete",
        }

        self.assertTrue(expected.issubset(routes))
        for path in expected:
            if path.startswith("/users/"):
                dependency_calls = {
                    dependency.call for dependency in routes[path].dependant.dependencies
                }
                self.assertIn(api.require_tasks_run, dependency_calls)

    def test_routes_use_explicit_response_models_without_register_number(self):
        routes = {getattr(route, "path", ""): route for route in api.router.routes}
        paths = {
            "/app/clock-in/preauthorizations",
            "/app/clock-in/preauthorizations/start",
            "/app/clock-in/preauthorizations/complete",
            "/users/{user_id}/clock-in/preauthorizations",
            "/users/{user_id}/clock-in/preauthorizations/start",
            "/users/{user_id}/clock-in/preauthorizations/complete",
        }

        for path in paths:
            model = routes[path].response_model
            self.assertIsNotNone(model, path)
            self.assertNotIn("out_register_no", str(model.model_json_schema()))

    def test_start_returns_two_urls_and_ticket_without_persisting(self):
        user = build_user()
        request_model = api.ClockInPreauthorizationStartRequest(
            target_date="2026-07-16",
            target_type="START",
        )
        config_data = {"config": {}, "userInfo": {}, "planInfo": user.planInfo}
        registration = {
            "outRegisterNo": "register-1",
            "registerUrl": "alipays://platformapi/startapp?appId=test",
        }

        with (
            patch.object(api, "validate_preauthorization_target") as validate,
            patch.object(api, "user_to_config", return_value=config_data),
            patch.object(api, "ApiClient") as api_client_type,
            patch.object(api, "_ensure_remote_runtime") as ensure_runtime,
            patch.object(api, "sync_runtime_fields_to_user") as sync_runtime,
            patch.object(api, "build_alipay_open_urls", return_value=("alipays://direct", "https://ds.alipay.com/?scheme=x")),
            patch.object(api, "issue_registration_ticket", return_value="signed-ticket"),
        ):
            api_client_type.return_value.create_alipay_clockin_verification.return_value = registration
            result = api._start_clockin_preauthorization_for_user(user, request_model)

        self.assertEqual(result["direct_url"], "alipays://direct")
        self.assertEqual(result["browser_url"], "https://ds.alipay.com/?scheme=x")
        self.assertEqual(result["registration_ticket"], "signed-ticket")
        self.assertNotIn("out_register_no", result)
        validate.assert_called()
        ensure_runtime.assert_called_once()
        sync_runtime.assert_called_once_with(user, config_data)

    def test_start_rejects_invalid_target_before_remote_registration(self):
        user = build_user()
        request_model = api.ClockInPreauthorizationStartRequest(
            target_date="2026-07-19",
            target_type="START",
        )

        with (
            patch.object(
                api,
                "validate_preauthorization_target",
                side_effect=ValueError("目标日期或类型不在预授权列表中"),
            ),
            patch.object(api, "ApiClient") as api_client_type,
        ):
            with self.assertRaises(ValueError):
                api._start_clockin_preauthorization_for_user(user, request_model)

        api_client_type.assert_not_called()

    def test_start_rate_limit_supports_bulk_future_authorization_on_both_surfaces(self):
        user = build_user()
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        req = api.ClockInPreauthorizationStartRequest(
            target_date="2026-07-16",
            target_type="START",
        )
        result = {
            "registration_ticket": "ticket",
            "direct_url": "alipays://direct",
            "browser_url": "https://ds.alipay.com/?scheme=x",
            "started_at": "2026-07-15T10:00:00+08:00",
            "expires_at": "2026-07-15T10:30:00+08:00",
        }

        with (
            patch.object(api, "_get_authed_app_user", return_value=Mock()),
            patch.object(api, "_get_bound_task_user", return_value=user),
            patch.object(api, "_rate_limit") as app_limit,
            patch.object(
                api,
                "_start_clockin_preauthorization_for_user",
                return_value=result,
            ),
        ):
            api.app_start_clockin_preauthorization(
                request=request,
                req=req,
                session=Mock(),
                payload={"sub": "app:1"},
            )

        with (
            patch.object(api, "_get_active_user_for_payload", return_value=user),
            patch.object(api, "_rate_limit") as admin_limit,
            patch.object(
                api,
                "_start_clockin_preauthorization_for_user",
                return_value=result,
            ),
        ):
            api.start_user_clockin_preauthorization(
                request=request,
                req=req,
                user_id=user.id,
                session=Mock(),
                operator={"sub": "admin:1"},
            )

        completed = {
            "id": 1,
            "target_date": "2026-07-16",
            "target_type": "START",
            "status": "authorized",
            "authorized_at": "2026-07-15T10:00:00+00:00",
            "consumed_at": None,
            "used_target_type": None,
        }
        complete_req = api.ClockInPreauthorizationCompleteRequest(
            registration_ticket="ticket"
        )
        with (
            patch.object(api, "_get_authed_app_user", return_value=Mock()),
            patch.object(api, "_get_bound_task_user", return_value=user),
            patch.object(api, "_rate_limit") as app_complete_limit,
            patch.object(
                api,
                "_complete_clockin_preauthorization_for_user",
                return_value=completed,
            ),
        ):
            api.app_complete_clockin_preauthorization(
                request=request,
                req=complete_req,
                session=Mock(),
                payload={"sub": "app:1"},
            )

        with (
            patch.object(api, "_get_active_user_for_payload", return_value=user),
            patch.object(api, "_rate_limit") as admin_complete_limit,
            patch.object(
                api,
                "_complete_clockin_preauthorization_for_user",
                return_value=completed,
            ),
        ):
            api.complete_user_clockin_preauthorization(
                request=request,
                req=complete_req,
                user_id=user.id,
                session=Mock(),
                operator={"sub": "admin:1"},
            )

        self.assertEqual(app_limit.call_args.kwargs["limit"], 30)
        self.assertEqual(admin_limit.call_args.kwargs["limit"], 30)
        self.assertEqual(app_complete_limit.call_args.kwargs["limit"], 30)
        self.assertEqual(admin_complete_limit.call_args.kwargs["limit"], 30)

    def test_complete_response_is_sanitized(self):
        row = ClockInPreauthorization(
            id=10,
            tenant_id="tenant-a",
            user_id=7,
            target_date=datetime.date(2026, 7, 16),
            target_type="START",
            status="authorized",
            out_register_no="register-secret",
            authorized_at=datetime.datetime(2026, 7, 15, 10, 0),
        )
        session = Mock()

        with patch.object(api, "complete_preauthorization", return_value=row):
            result = api._complete_clockin_preauthorization_for_user(
                session,
                build_user(),
                "signed-ticket",
            )

        self.assertEqual(result["status"], "authorized")
        self.assertEqual(result["target_type"], "START")
        self.assertEqual(result["authorized_at"], "2026-07-15T10:00:00+00:00")
        self.assertNotIn("out_register_no", result)
        self.assertNotIn("register-secret", str(result))

    def test_list_syncs_plan_when_end_time_is_missing(self):
        user = build_user()
        user.planInfo = {}
        session = Mock()
        expected = {"items": [], "total": 0}

        with (
            patch.object(api, "_sync_preauthorization_plan") as sync_plan,
            patch.object(api, "list_preauthorizations", return_value=expected) as list_items,
        ):
            result = api._list_clockin_preauthorizations_for_user(
                session,
                user,
                scope="future",
                page=1,
                page_size=20,
                status=None,
                target_type=None,
                refresh_plan=False,
            )

        self.assertIs(result, expected)
        sync_plan.assert_called_once_with(user, force=True)
        list_items.assert_called_once()

    def test_app_run_injects_user_preauthorization_hooks(self):
        user = build_user()
        hooks = Mock()
        session = Mock()
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

        with (
            patch.object(api, "_get_authed_app_user", return_value=Mock()),
            patch.object(api, "_get_bound_task_user", return_value=user),
            patch.object(api, "_rate_limit"),
            patch.object(api, "user_to_config", return_value={"config": {}}),
            patch.object(api, "build_preauthorization_hooks", return_value=hooks),
            patch.object(api, "run_task_by_config", return_value=[]) as run_task,
            patch.object(api, "apply_execution_results_to_user", return_value="Success"),
        ):
            api.app_run(
                request=request,
                session=session,
                payload={"sub": "app:1", "tenant_id": "tenant-a"},
                req=api.AppRunRequest(task_type="clock_in"),
            )

        self.assertIs(run_task.call_args.kwargs["preauthorization_hooks"], hooks)

    def test_makeup_helper_injects_user_preauthorization_hooks(self):
        user = build_user()
        hooks = Mock()
        config_data = {"config": {}, "userInfo": {}, "planInfo": {}}

        with (
            patch.object(api, "user_to_config", return_value=config_data),
            patch.object(api, "ApiClient") as api_client_type,
            patch.object(api, "_ensure_remote_runtime"),
            patch.object(api, "build_preauthorization_hooks", return_value=hooks),
            patch.object(
                api,
                "perform_clock_in_makeup",
                return_value={"status": "success"},
            ) as makeup,
            patch.object(api, "apply_execution_results_to_user"),
        ):
            api._makeup_clockin_for_user(user, ["2026-07-14"], "END")

        self.assertIs(makeup.call_args.kwargs["preauthorization_hooks"], hooks)
        self.assertTrue(api_client_type.return_value.enable_proxy.called)


if __name__ == "__main__":
    unittest.main()
