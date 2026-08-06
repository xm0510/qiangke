import importlib.util
import os
import tempfile
import unittest
import uuid
from pathlib import Path

DATABASE_FILE = Path(__file__).resolve().parents[1] / "database.py"


class AdminRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_env = {key: os.environ.get(key) for key in ("DB_PATH", "ADMIN_PHONE")}
        os.environ["DB_PATH"] = str(Path(self.tempdir.name) / "data.db")
        os.environ["ADMIN_PHONE"] = "15056587110"
        name = "database_test_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, DATABASE_FILE)
        self.db = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.db)
        self.db.init_db()

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def test_recovery_resets_only_admin_and_runs_once(self):
        admin = self.db.register_or_set_password("15056587110", "OldPass123")
        user = self.db.register_or_set_password("13900000000", "UserPass123")
        self.assertEqual(admin["user"]["is_admin"], 1)
        self.assertEqual(user["user"]["is_admin"], 0)

        self.assertTrue(self.db.apply_admin_recovery_password("15056587110", "NewPass456"))
        with self.assertRaises(ValueError):
            self.db.login_with_password("15056587110", "OldPass123")
        self.assertTrue(self.db.login_with_password("15056587110", "NewPass456")["token"])
        self.assertTrue(self.db.login_with_password("13900000000", "UserPass123")["token"])
        self.assertFalse(self.db.apply_admin_recovery_password("15056587110", "NewPass456"))

    def test_recovery_creates_missing_configured_admin(self):
        self.assertTrue(self.db.apply_admin_recovery_password("15056587110", "FreshPass789"))
        result = self.db.login_with_password("15056587110", "FreshPass789")
        self.assertEqual(result["user"]["is_admin"], 1)

    def test_login_repairs_the_single_configured_admin(self):
        self.db.register_or_set_password("15056587110", "AdminPass123")
        self.db.register_or_set_password("13900000000", "UserPass123")
        with self.db.db_cursor() as conn:
            conn.execute("UPDATE users SET is_admin=CASE WHEN phone=? THEN 1 ELSE 0 END", ("13900000000",))

        result = self.db.login_with_password("15056587110", "AdminPass123")
        self.assertEqual(result["user"]["is_admin"], 1)
        with self.db.db_cursor(commit=False) as conn:
            admins = conn.execute("SELECT phone FROM users WHERE is_admin=1").fetchall()
        self.assertEqual([row["phone"] for row in admins], ["15056587110"])


if __name__ == "__main__":
    unittest.main()
