import datetime
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from server.clockin_preauthorization import (
    build_alipay_open_urls,
    build_preauthorization_rows,
    issue_registration_ticket,
    parse_plan_end_date,
    verify_registration_ticket,
)
from server.models import ClockInPreauthorization, User
from server.time_utils import utc_now


RAW_ALIPAY_URL = (
    "alipays://platformapi/startapp?appId=2021003160674131"
    "&page=pages%2Fanxin-card-api%2Fanxin-card-api%3FjobId%3D54957596"
    "&outRegisterNo=register-1"
    "&sceneCode=SCENE_REGISTER"
    "&thirdPartSchema=taoshenghuo%3A%2F%2F"
)


class ClockInPreauthorizationModelTest(unittest.TestCase):
    def test_user_records_created_at_by_default(self):
        before = utc_now()

        user = User(phone="13800000000", password="encrypted")

        self.assertGreaterEqual(user.created_at, before)

    def test_preauthorization_defaults_to_authorized(self):
        authorized_at = utc_now()

        row = ClockInPreauthorization(
            user_id=7,
            target_date=datetime.date(2026, 7, 16),
            target_type="START",
            out_register_no="register-1",
            authorized_at=authorized_at,
        )

        self.assertEqual(row.tenant_id, "default")
        self.assertEqual(row.status, "authorized")
        self.assertEqual(row.authorized_at, authorized_at)
        self.assertIsNone(row.consumed_at)
        self.assertIsNone(row.used_target_type)

    def test_preauthorization_target_has_unique_constraint(self):
        table = ClockInPreauthorization.__table__
        constraints = {
            item.name: tuple(column.name for column in item.columns)
            for item in table.constraints
            if item.name
        }

        self.assertEqual(
            constraints["uq_clockinpreauthorization_target"],
            ("tenant_id", "user_id", "target_date", "target_type"),
        )


class ClockInPreauthorizationDomainTest(unittest.TestCase):
    def test_parse_plan_end_date_accepts_runtime_datetime(self):
        self.assertEqual(
            parse_plan_end_date({"endTime": "2026-12-31 23:59:59"}),
            datetime.date(2026, 12, 31),
        )

    def test_parse_plan_end_date_rejects_missing_or_invalid_value(self):
        for plan_info in ({}, {"endTime": ""}, {"endTime": "not-a-date"}):
            with self.subTest(plan_info=plan_info), self.assertRaises(ValueError):
                parse_plan_end_date(plan_info)

    def test_build_rows_splits_past_and_future(self):
        rows = build_preauthorization_rows(
            added_date=datetime.date(2026, 7, 13),
            plan_end_date=datetime.date(2026, 7, 16),
            today=datetime.date(2026, 7, 15),
            weekdays=[1, 2, 3, 4, 5],
            start_time="08:30",
            end_time="18:30",
        )

        self.assertEqual(
            [
                (row.target_date.isoformat(), row.target_type, row.target_time)
                for row in rows
            ],
            [
                ("2026-07-13", "MAKEUP", None),
                ("2026-07-14", "MAKEUP", None),
                ("2026-07-15", "START", "08:30"),
                ("2026-07-15", "END", "18:30"),
                ("2026-07-16", "START", "08:30"),
                ("2026-07-16", "END", "18:30"),
            ],
        )

    def test_build_rows_filters_disabled_weekdays_and_empty_range(self):
        rows = build_preauthorization_rows(
            added_date=datetime.date(2026, 7, 17),
            plan_end_date=datetime.date(2026, 7, 20),
            today=datetime.date(2026, 7, 17),
            weekdays=[1, 2, 3, 4, 5],
            start_time="08:30",
            end_time="18:30",
        )
        empty = build_preauthorization_rows(
            added_date=datetime.date(2026, 7, 21),
            plan_end_date=datetime.date(2026, 7, 20),
            today=datetime.date(2026, 7, 17),
            weekdays=[1, 2, 3, 4, 5],
            start_time="08:30",
            end_time="18:30",
        )

        self.assertEqual({row.target_date.weekday() + 1 for row in rows}, {1, 5})
        self.assertEqual(empty, [])

    def test_build_rows_accepts_string_weekdays_from_legacy_json(self):
        rows = build_preauthorization_rows(
            added_date=datetime.date(2026, 7, 20),
            plan_end_date=datetime.date(2026, 7, 20),
            today=datetime.date(2026, 7, 20),
            weekdays=["1"],
            start_time="08:30",
            end_time="18:30",
        )

        self.assertEqual([row.target_type for row in rows], ["START", "END"])

    def test_build_open_urls_replaces_nested_callback(self):
        started_at = datetime.datetime(
            2026,
            7,
            15,
            18,
            30,
            tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
        )

        direct_url, browser_url = build_alipay_open_urls(
            RAW_ALIPAY_URL,
            account="13800000000&admin=true",
            started_at=started_at,
        )

        direct = urlsplit(direct_url)
        direct_pairs = parse_qsl(direct.query, keep_blank_values=True)
        direct_query = dict(direct_pairs)
        callback = urlsplit(direct_query["thirdPartSchema"])
        callback_query = dict(parse_qsl(callback.query, keep_blank_values=True))
        browser_query = dict(parse_qsl(urlsplit(browser_url).query, keep_blank_values=True))

        self.assertEqual(direct.scheme, "alipays")
        self.assertEqual(direct.netloc, "platformapi")
        self.assertEqual(
            callback_query["query"],
            "你已经成功了，请返回点击我已完成授权，"
            "本次授权账号：13800000000&admin=true，"
            "本次授权时间：2026-07-15 18:30:00",
        )
        self.assertEqual(callback_query["from"], "zh")
        self.assertEqual(callback_query["to"], "en")
        self.assertEqual(sum(key == "thirdPartSchema" for key, _ in direct_pairs), 1)
        self.assertEqual(browser_query["scheme"], direct_url)

    def test_build_open_urls_rejects_non_alipay_scheme(self):
        with self.assertRaises(ValueError):
            build_alipay_open_urls(
                "https://example.com/path?thirdPartSchema=x",
                account="13800000000",
                started_at=datetime.datetime.now(datetime.timezone.utc),
            )

    def test_build_open_urls_collapses_duplicate_callback(self):
        source = RAW_ALIPAY_URL + "&thirdPartSchema=https%3A%2F%2Fevil.example"

        direct_url, _ = build_alipay_open_urls(
            source,
            account="13800000000",
            started_at=datetime.datetime.now(datetime.timezone.utc),
        )

        pairs = parse_qsl(urlsplit(direct_url).query, keep_blank_values=True)
        self.assertEqual(sum(key == "thirdPartSchema" for key, _ in pairs), 1)

    def test_registration_ticket_binds_user_target_and_expiry(self):
        started_at = datetime.datetime(2026, 7, 15, 10, 30, tzinfo=datetime.timezone.utc)
        with patch("server.auth._secret", return_value=b"test-secret" * 4), patch(
            "server.auth.time.time", return_value=1_000
        ):
            ticket = issue_registration_ticket(
                tenant_id="tenant-a",
                user_id=7,
                target_date=datetime.date(2026, 7, 16),
                target_type="START",
                out_register_no="register-1",
                started_at=started_at,
            )
            claims = verify_registration_ticket(
                ticket,
                tenant_id="tenant-a",
                user_id=7,
            )

        self.assertEqual(claims["purpose"], "clockin_preauthorization")
        self.assertEqual(claims["target_date"], "2026-07-16")
        self.assertEqual(claims["target_type"], "START")
        self.assertEqual(claims["out_register_no"], "register-1")
        self.assertEqual(claims["started_at"], started_at.isoformat())

        with patch("server.auth._secret", return_value=b"test-secret" * 4):
            with self.assertRaises(ValueError):
                verify_registration_ticket(ticket, tenant_id="tenant-a", user_id=8)

        with patch("server.auth._secret", return_value=b"test-secret" * 4), patch(
            "server.auth.time.time", return_value=2_801
        ):
            with self.assertRaises(ValueError):
                verify_registration_ticket(ticket, tenant_id="tenant-a", user_id=7)

    def test_registration_ticket_rejects_tampering(self):
        with patch("server.auth._secret", return_value=b"test-secret" * 4):
            ticket = issue_registration_ticket(
                tenant_id="tenant-a",
                user_id=7,
                target_date=datetime.date(2026, 7, 16),
                target_type="START",
                out_register_no="register-1",
                started_at=datetime.datetime.now(datetime.timezone.utc),
            )
            payload, signature = ticket.split(".", 1)

            with self.assertRaises(ValueError):
                verify_registration_ticket(
                    f"{payload}x.{signature}",
                    tenant_id="tenant-a",
                    user_id=7,
                )


if __name__ == "__main__":
    unittest.main()
