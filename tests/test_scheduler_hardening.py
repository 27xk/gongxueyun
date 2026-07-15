import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine


class SchedulerHardeningTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        import server.models  # noqa: F401 - 加载 SQLModel 元数据

        SQLModel.metadata.create_all(self.engine)

    def test_iter_schedulable_users_pages_and_skips_users_without_jobs(self):
        from server.models import Tenant, User
        from server.scheduler import _iter_schedulable_users
        from server.time_utils import utc_now

        with Session(self.engine) as session:
            disabled_tenant = Tenant(id="disabled", name="Disabled", status="disabled")
            active_clockin = User(phone="13800000001", password="encrypted", enable_clockin=True)
            active_report = User(
                phone="13800000002",
                password="encrypted",
                enable_clockin=False,
                reportSettings={"daily": {"enabled": True}},
            )
            no_jobs = User(phone="13800000003", password="encrypted", enable_clockin=False, reportSettings={})
            deleted = User(phone="13800000004", password="encrypted", enable_clockin=True, deleted_at=utc_now())
            disabled_tenant_user = User(tenant_id="disabled", phone="13800000006", password="encrypted", enable_clockin=True)
            session.add(disabled_tenant)
            session.add(active_clockin)
            session.add(active_report)
            session.add(no_jobs)
            session.add(deleted)
            session.add(disabled_tenant_user)
            session.commit()

            users = list(_iter_schedulable_users(session, page_size=1))

        self.assertEqual([user.phone for user in users], ["13800000001", "13800000002"])

    def test_run_job_skips_disabled_tenant(self):
        from server.models import Tenant, User
        from server.scheduler import run_job

        with Session(self.engine) as session:
            session.add(Tenant(id="disabled", name="Disabled", status="disabled"))
            user = User(tenant_id="disabled", phone="13800000007", password="encrypted", enable_clockin=True)
            session.add(user)
            session.commit()
            session.refresh(user)
            user_id = user.id

        with (
            patch("server.scheduler.engine", self.engine),
            patch("server.scheduler.acquire_task_lock", return_value=object()),
            patch("server.scheduler.release_task_lock"),
            patch("server.scheduler.record_task_event"),
            patch("server.scheduler.run_task_by_config", return_value={"status": "Success"}) as run_task,
        ):
            run_job(user_id, "START")

        run_task.assert_not_called()

    def test_run_report_job_skips_disabled_report_settings(self):
        from server.models import User
        from server.scheduler import run_report_job

        with Session(self.engine) as session:
            user = User(
                phone="13800000005",
                password="encrypted",
                enable_clockin=False,
                reportSettings={"daily": {"enabled": False}},
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            user_id = user.id

        with (
            patch("server.scheduler.engine", self.engine),
            patch("server.scheduler.acquire_task_lock", return_value=object()),
            patch("server.scheduler.release_task_lock"),
            patch("server.scheduler.record_task_event"),
            patch("server.scheduler.run_task_by_config", return_value={"status": "Success"}) as run_task,
        ):
            run_report_job(user_id, "daily_report")

        run_task.assert_not_called()

    def test_run_clockin_job_injects_preauthorization_hooks(self):
        from server.models import User
        from server.scheduler import run_job

        with Session(self.engine) as session:
            user = User(
                phone="13800000008",
                password="encrypted",
                enable_clockin=True,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            user_id = user.id

        hooks = object()
        with (
            patch("server.scheduler.engine", self.engine),
            patch("server.scheduler.acquire_task_lock", return_value=object()),
            patch("server.scheduler.release_task_lock"),
            patch("server.scheduler.record_task_event"),
            patch("server.scheduler.build_preauthorization_hooks", return_value=hooks),
            patch("server.scheduler.apply_execution_results_to_user"),
            patch(
                "server.scheduler.run_task_by_config",
                return_value=[{"status": "success"}],
            ) as run_task,
        ):
            run_job(user_id, "START")

        self.assertIs(run_task.call_args.kwargs["preauthorization_hooks"], hooks)


if __name__ == "__main__":
    unittest.main()
