import datetime
import unittest

from sqlmodel import SQLModel, create_engine


class ExecutionLockTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def test_lock_blocks_second_owner_until_released(self):
        from server.execution_locks import acquire_task_lock, release_task_lock

        first = acquire_task_lock(
            "scheduler:user:1:clock_in:START",
            ttl_seconds=60,
            owner="worker-a",
            db_engine=self.engine,
        )
        second = acquire_task_lock(
            "scheduler:user:1:clock_in:START",
            ttl_seconds=60,
            owner="worker-b",
            db_engine=self.engine,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)

        self.assertTrue(release_task_lock(first, db_engine=self.engine))
        third = acquire_task_lock(
            "scheduler:user:1:clock_in:START",
            ttl_seconds=60,
            owner="worker-b",
            db_engine=self.engine,
        )
        self.assertIsNotNone(third)

    def test_expired_lock_can_be_reclaimed(self):
        from server.execution_locks import acquire_task_lock

        now = datetime.datetime(2026, 1, 1, 8, 0, 0)
        old = acquire_task_lock(
            "scheduler:user:2:report:daily",
            ttl_seconds=1,
            owner="worker-a",
            now=now,
            db_engine=self.engine,
        )
        reclaimed = acquire_task_lock(
            "scheduler:user:2:report:daily",
            ttl_seconds=60,
            owner="worker-b",
            now=now + datetime.timedelta(seconds=2),
            db_engine=self.engine,
        )

        self.assertIsNotNone(old)
        self.assertIsNotNone(reclaimed)
        self.assertNotEqual(old.owner, reclaimed.owner)


if __name__ == "__main__":
    unittest.main()
