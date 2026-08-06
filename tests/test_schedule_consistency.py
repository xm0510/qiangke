import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


class ScheduleConsistencyTests(unittest.TestCase):
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
        response = self.client.post("/api/auth/register", json={
            "phone": "15056587110",
            "password": "AdminPass123",
            "invite_code": "InviteCode2026",
        })
        self.assertEqual(response.status_code, 200)
        self.user_id = response.get_json()["user"]["id"]
        self.server.db.set_config("conflict_detection", "true", self.user_id)

    def tearDown(self):
        sys.modules.pop("server", None)
        sys.modules.pop("database", None)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def add_course(self, day, start, name):
        response = self.client.post("/api/schedule", json={
            "student_name": name,
            "subject_type": "Reading",
            "day_of_week": day,
            "start_time": start,
            "duration_min": 60,
            "status": "confirmed",
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["id"]

    def test_edit_conflict_override_updates_instead_of_inserting(self):
        first_id = self.add_course(1, "09:00", "Student A")
        second_id = self.add_course(2, "11:00", "Student B")

        conflict = self.client.put(f"/api/schedule/{second_id}", json={
            "day_of_week": 1,
            "start_time": "09:30",
            "duration_min": 60,
        })
        self.assertEqual(conflict.status_code, 200)
        self.assertTrue(conflict.get_json()["conflict"])

        saved = self.client.put(f"/api/schedule/{second_id}", json={
            "day_of_week": 1,
            "start_time": "09:30",
            "duration_min": 60,
            "_skip_conflict": True,
        })
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        entries = self.client.get("/api/schedule").get_json()
        self.assertEqual(len(entries), 2)
        updated = next(item for item in entries if item["id"] == second_id)
        self.assertEqual(updated["start_time"], "09:30")
        self.assertTrue(any(item["id"] == first_id for item in entries))

    def test_delete_route_cascades_generated_reviews(self):
        parent_id = self.add_course(3, "13:00", "Student C")
        review_id = self.server.db.add_schedule_entry({
            "user_id": self.user_id,
            "student_name": "Student C",
            "subject_type": "Review",
            "day_of_week": 4,
            "start_time": "13:00",
            "duration_min": 30,
            "status": "pending",
            "source": "review",
            "parent_entry_id": parent_id,
        })

        deleted = self.client.delete(f"/api/schedule/{parent_id}")
        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        self.assertIsNone(self.server.db.get_schedule_entry(parent_id, self.user_id))
        self.assertIsNone(self.server.db.get_schedule_entry(review_id, self.user_id))

    def test_delete_transaction_rolls_back_children_if_parent_delete_fails(self):
        parent_id = self.add_course(5, "15:00", "Student D")
        review_id = self.server.db.add_schedule_entry({
            "user_id": self.user_id,
            "student_name": "Student D",
            "subject_type": "Review",
            "day_of_week": 6,
            "start_time": "15:00",
            "duration_min": 30,
            "status": "pending",
            "source": "review",
            "parent_entry_id": parent_id,
        })
        with self.server.db.db_cursor() as conn:
            conn.execute(f"""
                CREATE TRIGGER block_parent_delete
                BEFORE DELETE ON schedule_entries
                WHEN OLD.id={parent_id}
                BEGIN
                    SELECT RAISE(ABORT, 'blocked');
                END
            """)

        with self.assertRaises(sqlite3.DatabaseError):
            self.server.db.delete_schedule_entry(parent_id, self.user_id)
        self.assertIsNotNone(self.server.db.get_schedule_entry(parent_id, self.user_id))
        self.assertIsNotNone(self.server.db.get_schedule_entry(review_id, self.user_id))


class ScheduleUiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parents[1] / "web" / "schedule.html").read_text(encoding="utf-8")

    def test_mobile_grid_can_shrink_without_clipping_content(self):
        self.assertIn(".layout{grid-template-columns:minmax(0,1fr)}", self.source)
        self.assertIn(".layout>*,.card,.body{min-width:0;max-width:100%}", self.source)
        self.assertIn(".scroll{width:100%;max-width:100%;overflow-x:auto}", self.source)

    def test_conflict_override_preserves_edit_method_and_url(self):
        self.assertIn("var method=id?'PUT':'POST'", self.source)
        self.assertIn("var url=id?'/api/schedule/'+id:'/api/schedule'", self.source)
        delete_code = self.source.split("async function deleteCourse", 1)[1].split("async function changeStatus", 1)[0]
        self.assertNotIn("/reviews", delete_code)

    def test_demo_is_read_only_and_reminders_use_backoff(self):
        self.assertIn("var demoMode=", self.source)
        self.assertIn("function buildDemoEntries()", self.source)
        self.assertIn("document.addEventListener('visibilitychange'", self.source)
        self.assertIn("reminderInFlight", self.source)
        self.assertNotIn("setInterval(loadCourseReminders,5000)", self.source)


if __name__ == "__main__":
    unittest.main()
