import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


class P1ExperienceTests(unittest.TestCase):
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
        self.client = self.server.app.test_client()
        result = self.client.post("/api/auth/register", json={
            "phone": "15056587110", "password": "AdminPass123", "invite_code": "InviteCode2026"
        })
        self.assertEqual(result.status_code, 200)

    def tearDown(self):
        sys.modules.pop("server", None)
        sys.modules.pop("database", None)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def test_information_architecture_routes(self):
        expected = {
            "/": 'href="/schedule?demo=1"',
            "/schedule": 'id="emptyState"',
            "/feedback": 'id="candidateGrid"',
            "/automation": 'id="localAssistantCard"',
            "/admin": 'id="adminContent"',
        }

        for path, marker in expected.items():
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.get_data(as_text=True)
            response.close()
            self.assertIn(marker, body, path)

    def test_local_assistant_boundary_is_explicit(self):
        response = self.client.get("/api/local-assistant/status")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["architecture"], "cloud-plus-windows-helper")
        self.assertFalse(data["assistant_online"])
        self.assertFalse(data["wechat_connected"])
        self.assertFalse(data["ocr_available"])

    def test_schedule_validates_single_and_weekly_modes(self):
        base = {"student_name": "Student", "subject_type": "Reading", "start_time": "19:30",
                "duration_min": 60, "price_per_session": 100, "status": "confirmed"}
        missing_date = self.client.post("/api/schedule", json={**base, "day_of_week": 1, "is_recurring": 0})
        self.assertEqual(missing_date.status_code, 400)
        single = self.client.post("/api/schedule", json={**base, "day_of_week": 1,
            "is_recurring": 0, "schedule_date": "2026-08-10"})
        self.assertEqual(single.status_code, 200, single.get_data(as_text=True))
        missing_end = self.client.post("/api/schedule", json={**base, "day_of_week": 2, "is_recurring": 1})
        self.assertEqual(missing_end.status_code, 400)
        weekly = self.client.post("/api/schedule", json={**base, "day_of_week": 2,
            "is_recurring": 1, "recur_until_count": 10})
        self.assertEqual(weekly.status_code, 200, weekly.get_data(as_text=True))

    def test_schedule_rejects_invalid_ranges(self):
        base = {"student_name": "Student", "subject_type": "Reading", "day_of_week": 1,
                "start_time": "19:30", "duration_min": 60, "price_per_session": 100,
                "status": "pending", "is_recurring": 0, "schedule_date": "2026-08-10"}
        for field, value in (("duration_min", 9), ("duration_min", 481), ("price_per_session", -1)):
            response = self.client.post("/api/schedule", json={**base, field: value})
            self.assertEqual(response.status_code, 400, (field, value, response.get_data(as_text=True)))

    def test_review_intervals_must_be_unique_positive_integers(self):
        course = self.client.post("/api/schedule", json={"student_name": "Student", "subject_type": "Reading",
            "day_of_week": 1, "start_time": "19:30", "duration_min": 60, "price_per_session": 100,
            "status": "pending", "is_recurring": 0, "schedule_date": "2026-08-10"}).get_json()
        response = self.client.post(f"/api/schedule/{course['id']}/generate-reviews", json={
            "intervals": [1, 1, 0], "schedule_date": "2026-08-10", "start_time": "20:00", "duration_min": 30
        })
        self.assertEqual(response.status_code, 400)

    def test_schedule_page_contains_p1_controls(self):
        html = (Path(__file__).resolve().parents[1] / "web" / "schedule.html").read_text(encoding="utf-8")
        for marker in ('id="emptyState"', 'id="courseMode"', 'role="dialog"',
                       'aria-modal="true"', "function exportPng", "function exportPdf",
                       "function exportCsv", "function exportIcs", "function importCsv",
                       'min="10" max="480"', "min-height:44px"):
            self.assertIn(marker, html)

    def test_config_page_reports_save_and_accessible_switches(self):
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ('id="saveStatus"', "savedConfig", "pendingSave", "retrySave",
                       "loadLocalAssistantStatus", "setupToggleLabels", "opacity:0"):
            self.assertIn(marker, html)
        self.assertNotIn(".toggle input { display:none; }", html)


if __name__ == "__main__":
    unittest.main()
