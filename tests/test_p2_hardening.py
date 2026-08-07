import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


class P2HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_env = {key: os.environ.get(key) for key in (
            "DB_PATH", "ADMIN_PHONE", "REGISTRATION_INVITE_CODE", "ADMIN_RECOVERY_PASSWORD"
        )}
        os.environ["DB_PATH"] = str(Path(self.tempdir.name) / "data.db")
        os.environ["ADMIN_PHONE"] = "15056587110"
        os.environ["REGISTRATION_INVITE_CODE"] = "InviteCode2026"
        os.environ.pop("ADMIN_RECOVERY_PASSWORD", None)
        sys.modules.pop("server", None)
        sys.modules.pop("database", None)
        self.server = importlib.import_module("server")
        self.admin = self.server.app.test_client()
        self.normal = self.server.app.test_client()
        admin_register = self.admin.post("/api/auth/register", json={
            "phone": "15056587110", "password": "AdminPass123", "invite_code": "InviteCode2026"
        })
        normal_register = self.normal.post("/api/auth/register", json={
            "phone": "13900000001", "password": "UserPass123", "invite_code": "InviteCode2026"
        })
        self.assertEqual(admin_register.status_code, 200)
        self.assertEqual(normal_register.status_code, 200)

    def tearDown(self):
        sys.modules.pop("server", None)
        sys.modules.pop("database", None)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def add_course(self, client, **overrides):
        data = {
            "student_name": "Student", "subject_type": "Reading", "day_of_week": 1,
            "start_time": "19:30", "duration_min": 60, "price_per_session": 100,
            "status": "pending", "is_recurring": 0, "schedule_date": "2026-08-10",
        }
        data.update(overrides)
        response = client.post("/api/schedule", json=data)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["id"]

    def test_security_headers_and_favicon(self):
        response = self.admin.get("/", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("max-age=31536000", response.headers["Strict-Transport-Security"])
        response.close()
        icon = self.admin.get("/favicon.svg")
        self.assertEqual(icon.status_code, 200)
        self.assertIn("image/svg+xml", icon.content_type)
        self.assertIn(b"<svg", icon.data)
        icon.close()

    def test_local_wechat_actions_require_admin(self):
        for path in (
            "/api/service/start", "/api/service/stop", "/api/service/pause",
            "/api/service/resume", "/api/test-reply", "/api/groups/scan", "/api/diagnose",
        ):
            response = self.normal.post(path, json={})
            self.assertEqual(response.status_code, 403, (path, response.get_data(as_text=True)))
            self.assertEqual(response.get_json()["error"], "forbidden")

    def test_review_preview_reports_only_current_users_conflicts(self):
        parent_id = self.add_course(self.admin)
        self.add_course(self.admin, student_name="Admin conflict", day_of_week=2,
                        schedule_date="2026-08-11", start_time="19:45", duration_min=30)
        self.add_course(self.normal, student_name="Other user", day_of_week=2,
                        schedule_date="2026-08-11", start_time="19:30", duration_min=60)
        payload = {"intervals": [1], "schedule_date": "2026-08-10",
                   "start_time": "19:30", "duration_min": 60}
        preview = self.admin.post(f"/api/schedule/{parent_id}/review-preview", json=payload)
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        data = preview.get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["date"], "2026-08-11")
        self.assertEqual(data["conflict_count"], 1)
        self.assertEqual(data["items"][0]["conflicts"][0]["student_name"], "Admin conflict")

        blocked = self.admin.post(f"/api/schedule/{parent_id}/generate-reviews", json=payload)
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(blocked.get_json()["conflict"])
        allowed = self.admin.post(f"/api/schedule/{parent_id}/generate-reviews",
                                  json={**payload, "_skip_conflict": True})
        self.assertEqual(allowed.status_code, 200, allowed.get_data(as_text=True))
        self.assertEqual(allowed.get_json()["created"], 1)

    def test_work_hours_config_is_user_isolated(self):
        saved = self.admin.post("/api/config", json={
            "schedule_work_start": "08:00", "schedule_work_end": "22:00"
        })
        self.assertEqual(saved.status_code, 200)
        admin_config = self.admin.get("/api/config").get_json()
        normal_config = self.normal.get("/api/config").get_json()
        self.assertEqual(admin_config["schedule_work_start"], "08:00")
        self.assertNotIn("schedule_work_start", normal_config)

    def test_p2_frontend_markers(self):
        root = Path(__file__).resolve().parents[1]
        schedule = (root / "web" / "schedule.html").read_text(encoding="utf-8")
        automation = (root / "web" / "index.html").read_text(encoding="utf-8")
        for marker in (
            'id="workStart"', 'id="workEnd"', "schedule_work_start",
            'id="confirmModal"', "function askConfirm", 'id="reviewPreview"',
            "/review-preview", "conflict_count", '<button type="button" class="entry',
            '<button type="button" aria-label="',
        ):
            self.assertIn(marker, schedule)
        self.assertNotIn("if(confirm(", schedule)
        for marker in ('id="serviceControls"', "currentUserIsAdmin", "admin-local-control"):
            self.assertIn(marker, automation)
        self.assertNotIn("display:grid!important", automation)
        for page in (root / "web").glob("*.html"):
            self.assertIn('/favicon.svg', page.read_text(encoding="utf-8"), page.name)


if __name__ == "__main__":
    unittest.main()
