import datetime
import unittest

from server.models import ClockInPreauthorization, User
from server.time_utils import utc_now


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


if __name__ == "__main__":
    unittest.main()
