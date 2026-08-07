import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


class BetaReadinessTests(unittest.TestCase):
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
        self.other = self.server.app.test_client()
        first = self.client.post("/api/auth/register", json={
            "phone": "15056587110", "password": "AdminPass123", "invite_code": "InviteCode2026"
        })
        second = self.other.post("/api/auth/register", json={
            "phone": "13900000001", "password": "UserPass123", "invite_code": "InviteCode2026"
        })
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

    def tearDown(self):
        sys.modules.pop("server", None)
        sys.modules.pop("database", None)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    @staticmethod
    def rows():
        return [
            {"student_name": "Student A", "subject_type": "Math", "schedule_date": "2026-08-10",
             "start_time": "09:00", "duration_min": "60", "price_per_session": "100",
             "status": "pending", "notes": "Algebra"},
            {"student_name": "Student B", "subject_type": "English", "schedule_date": "2026-08-11",
             "start_time": "10:30", "duration_min": "45", "price_per_session": "120",
             "status": "completed", "notes": "Reading"},
        ]

    def test_import_requires_critical_fields_with_row_number(self):
        rows = self.rows()
        del rows[0]["schedule_date"]
        response = self.client.post("/api/schedule/import", json={
            "importId": str(uuid.uuid4()), "rows": rows, "commit": False,
        })
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["errors"][0]["row"], 2)
        self.assertIn("\u4e0a\u8bfe\u65e5\u671f", data["errors"][0]["error"])
        self.assertEqual(self.client.get("/api/schedule").get_json(), [])

    def test_import_is_atomic_idempotent_and_undoable(self):
        import_id = str(uuid.uuid4())
        payload = {"importId": import_id, "rows": self.rows()}
        preview = self.client.post("/api/schedule/import", json={**payload, "commit": False})
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        self.assertEqual(preview.get_json()["count"], 2)

        committed = self.client.post("/api/schedule/import", json={**payload, "commit": True})
        self.assertEqual(committed.status_code, 200, committed.get_data(as_text=True))
        self.assertEqual(committed.get_json()["created"], 2)
        self.assertFalse(committed.get_json()["duplicate"])
        self.assertEqual(len(self.client.get("/api/schedule").get_json()), 2)

        retried = self.client.post("/api/schedule/import", json={**payload, "commit": True})
        self.assertEqual(retried.status_code, 200)
        self.assertTrue(retried.get_json()["duplicate"])
        self.assertEqual(retried.get_json()["created"], 0)
        repeated_file = self.client.post("/api/schedule/import", json={
            "importId": str(uuid.uuid4()), "rows": self.rows(), "commit": True,
        })
        self.assertTrue(repeated_file.get_json()["duplicate"])
        self.assertEqual(len(self.client.get("/api/schedule").get_json()), 2)
        self.assertIsNone(self.other.get("/api/schedule/import/latest").get_json()["batch"])

        undone = self.client.delete("/api/schedule/import/latest")
        self.assertEqual(undone.status_code, 200, undone.get_data(as_text=True))
        self.assertEqual(undone.get_json()["deleted"], 2)
        self.assertEqual(self.client.get("/api/schedule").get_json(), [])

    def test_import_transaction_rolls_back_every_row(self):
        with self.server.db.db_cursor() as conn:
            conn.execute("""
                CREATE TRIGGER block_second_import
                BEFORE INSERT ON schedule_entries
                WHEN NEW.student_name='Student B'
                BEGIN SELECT RAISE(ABORT, 'blocked'); END
            """)
        import_id = str(uuid.uuid4())
        committed = self.client.post("/api/schedule/import", json={
            "importId": import_id, "rows": self.rows(), "commit": True,
        })
        self.assertEqual(committed.status_code, 500, committed.get_data(as_text=True))
        self.assertIn("\u6ca1\u6709\u5199\u5165\u4efb\u4f55\u8bfe\u7a0b", committed.get_json()["error"])
        self.assertEqual(self.client.get("/api/schedule").get_json(), [])
        self.assertIsNone(self.client.get("/api/schedule/import/latest").get_json()["batch"])

    def test_calendar_uses_rfc_fields_crlf_and_text_escaping(self):
        created = self.client.post("/api/schedule", json={
            "student_name": "A,B;C", "subject_type": "Math", "schedule_date": "2026-08-10",
            "day_of_week": 1, "start_time": "19:30", "duration_min": 60,
            "price_per_session": 100, "status": "pending", "is_recurring": 0,
            "notes": "line1\\path;value,more\nline2",
        })
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        course_id = created.get_json()["id"]
        response = self.client.get(f"/api/schedule/{course_id}/calendar.ics?date=2026-08-10")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        text = response.data.decode("utf-8")
        self.assertIn("\r\n", text)
        self.assertIn("UID:", text)
        self.assertIn("DTSTAMP:", text)
        self.assertRegex(text, r"DTSTART:\d{8}T\d{6}Z")
        self.assertIn(r"A\,B\;C", text)
        self.assertIn(r"line1\\path\;value\,more\nline2", text)


class BetaReadinessUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.schedule = (cls.root / "web" / "schedule.html").read_text(encoding="utf-8")

    def test_schedule_has_preview_mobile_demo_and_safe_exports(self):
        for marker in (
            'id="importModal"', "function commitImport", "/api/schedule/import",
            "function renderMobileWeekList", "schedule-week-view", "min-height:44px",
            'id="demoCta"', "demoAuthenticated", "function safeCsvCell",
            "DTSTAMP:", "function icsEscape", "function wrapCanvasText",
            r"\u6dfb\u52a0\u5230\u624b\u673a\u65e5\u5386", "downloadImportTemplate()",
        ):
            if ";" in marker:
                self.assertTrue(all(part in self.schedule for part in marker.split(";")), marker)
            else:
                self.assertIn(marker, self.schedule)
        self.assertNotIn("/api/schedule/'+e.id+'/calendar.ics", self.schedule)
        self.assertEqual(self.schedule.count("async function importCsv"), 1)
        self.assertNotIn(r"\u590d\u5236\u672c\u5468\u8bfe\u8868\u6587\u672c", self.schedule)

    def test_private_pages_are_noindex_and_landing_is_indexable(self):
        for name in ("schedule.html", "feedback.html", "index.html", "login.html", "admin.html"):
            source = (self.root / "web" / name).read_text(encoding="utf-8")
            self.assertIn('name="robots" content="noindex,nofollow"', source, name)
        landing = (self.root / "web" / "landing.html").read_text(encoding="utf-8")
        self.assertNotIn('name="robots" content="noindex,nofollow"', landing)
        for marker in ('name="description"', 'rel="canonical"', 'property="og:title"',
                       'id="faq"', 'href="/privacy"', 'href="/terms"'):
            self.assertIn(marker, landing)


if __name__ == "__main__":
    unittest.main()
