import datetime
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import Session, select
from sqlmodel import SQLModel, create_engine


class ObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def test_records_task_event_and_returns_metrics(self):
        from server.observability import record_task_event, runtime_metrics

        record_task_event(
            source="scheduler",
            event="finish",
            task_key="scheduler:user:1:clock_in:START",
            user_id=1,
            status="success",
            duration_ms=12,
            db_engine=self.engine,
        )

        metrics = runtime_metrics(db_engine=self.engine)

        self.assertEqual(metrics["task_events"]["by_status"]["success"], 1)
        self.assertIn("generated_at", metrics)

    def test_prometheus_metrics_text_is_exposed(self):
        from server.models import AuditLog
        from server.observability import prometheus_metrics_text, record_task_event

        record_task_event(
            source="scheduler",
            event="finish",
            task_key="scheduler:user:1:clock_in:START",
            user_id=1,
            status="success",
            duration_ms=12,
            db_engine=self.engine,
        )
        with Session(self.engine) as session:
            session.add(AuditLog(actor="auth:failed", action="auth.login.failed", detail={"reason": "invalid_password"}))
            session.commit()

        text = prometheus_metrics_text(db_engine=self.engine)

        self.assertIn('automoguding_task_events_total{status="success"} 1', text)
        self.assertIn('automoguding_auth_failures_total{action="auth.login.failed"} 1', text)
        self.assertIn("automoguding_locks_active", text)

    def test_recent_auth_failure_metrics_exclude_old_audit_rows(self):
        from server.models import AuditLog
        from server.observability import prometheus_metrics_text, runtime_metrics

        now = datetime.datetime(2026, 5, 29, 12, 0, tzinfo=datetime.timezone.utc)
        with Session(self.engine) as session:
            session.add(
                AuditLog(
                    created_at=now - datetime.timedelta(minutes=2),
                    actor="auth:failed",
                    action="auth.login.failed",
                    detail={"reason": "invalid_password"},
                )
            )
            session.add(
                AuditLog(
                    created_at=now - datetime.timedelta(minutes=30),
                    actor="auth:failed",
                    action="auth.login.failed",
                    detail={"reason": "old_invalid_password"},
                )
            )
            session.commit()

        with patch("server.observability._now_utc", return_value=now):
            metrics = runtime_metrics(db_engine=self.engine)
            text = prometheus_metrics_text(db_engine=self.engine)

        self.assertEqual(metrics["auth_failures"]["by_action"]["auth.login.failed"], 2)
        self.assertEqual(metrics["auth_failures"]["recent_by_action"]["auth.login.failed"], 1)
        self.assertIn('automoguding_auth_failures_recent_total{action="auth.login.failed"} 1', text)

    def test_recent_http_request_metrics_exclude_old_rows(self):
        from server.models import HttpRequestMetric
        from server.observability import prometheus_metrics_text, runtime_metrics

        now = datetime.datetime(2026, 5, 29, 12, 0, tzinfo=datetime.timezone.utc)
        with Session(self.engine) as session:
            session.add(
                HttpRequestMetric(
                    created_at=now - datetime.timedelta(minutes=2),
                    method="GET",
                    path="/api/current-error",
                    status_code=500,
                    duration_ms=40,
                )
            )
            session.add(
                HttpRequestMetric(
                    created_at=now - datetime.timedelta(minutes=30),
                    method="GET",
                    path="/api/old-error",
                    status_code=500,
                    duration_ms=20,
                )
            )
            session.add(
                HttpRequestMetric(
                    created_at=now - datetime.timedelta(minutes=1),
                    method="GET",
                    path="/api/users",
                    status_code=200,
                    duration_ms=10,
                )
            )
            session.commit()

        with patch("server.observability._now_utc", return_value=now):
            metrics = runtime_metrics(db_engine=self.engine)
            text = prometheus_metrics_text(db_engine=self.engine)

        self.assertEqual(metrics["http_requests"]["by_status"]["5xx"], 2)
        self.assertEqual(metrics["http_requests"]["recent_by_status"]["5xx"], 1)
        self.assertEqual(metrics["http_requests"]["recent_by_status"]["2xx"], 1)
        self.assertIn('automoguding_http_requests_recent_total{status="5xx"} 1', text)

    def test_runtime_metrics_uses_short_lived_cache(self):
        from server.models import TaskExecutionEvent
        import server.observability as observability

        with Session(self.engine) as session:
            session.add(TaskExecutionEvent(source="api", event="finish", task_key="manual:1", status="success"))
            session.commit()

        with patch.dict("os.environ", {"METRICS_CACHE_TTL_SECONDS": "60"}, clear=False):
            with patch.object(observability, "_runtime_metrics_snapshot", wraps=observability._runtime_metrics_snapshot) as snapshot:
                with patch("server.observability.time.monotonic", return_value=100.0):
                    first = observability.runtime_metrics(db_engine=self.engine)

                with patch("server.observability.time.monotonic", return_value=101.0):
                    cached = observability.runtime_metrics(db_engine=self.engine)

                with Session(self.engine) as session:
                    session.add(TaskExecutionEvent(source="api", event="finish", task_key="manual:2", status="success"))
                    session.commit()

                with patch("server.observability.time.monotonic", return_value=102.0):
                    refreshed = observability.runtime_metrics(db_engine=self.engine)

        self.assertEqual(first["task_events"]["by_status"]["success"], 1)
        self.assertEqual(cached["task_events"]["by_status"]["success"], 1)
        self.assertEqual(refreshed["task_events"]["by_status"]["success"], 2)
        self.assertEqual(snapshot.call_count, 2)

    def test_static_and_health_paths_do_not_write_http_request_metrics(self):
        from server.models import HttpRequestMetric
        from server.observability import record_http_request

        record_http_request(method="GET", path="/assets/index.js", status_code=200, duration_ms=1, db_engine=self.engine)
        record_http_request(method="GET", path="/healthz", status_code=200, duration_ms=1, db_engine=self.engine)
        record_http_request(method="GET", path="/api/users", status_code=200, duration_ms=3, db_engine=self.engine)

        with Session(self.engine) as session:
            rows = session.exec(select(HttpRequestMetric)).all()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].path, "/api/users")

    def test_prometheus_alert_rules_cover_core_operational_risks(self):
        rules = (Path(__file__).resolve().parents[1] / "monitoring" / "prometheus" / "alerts.yml").read_text(encoding="utf-8")

        self.assertIn("AutoMoGuDingHighLoginFailureRate", rules)
        self.assertIn("AutoMoGuDingBatchQueueBacklog", rules)
        self.assertIn("AutoMoGuDingHighHttp5xxRate", rules)
        self.assertIn("automoguding_auth_failures_recent_total", rules)
        self.assertIn("automoguding_http_requests_recent_total", rules)
        self.assertNotIn("increase(automoguding_auth_failures_total", rules)
        self.assertNotIn("increase(automoguding_http_requests_total", rules)


if __name__ == "__main__":
    unittest.main()
