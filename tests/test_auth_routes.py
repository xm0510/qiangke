import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


class AuthRouteTests(unittest.TestCase):
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
        self.admin_client = self.server.app.test_client()
        self.user_client = self.server.app.test_client()
        self.admin_client.post("/api/auth/register", json={
            "phone": "15056587110", "password": "AdminPass123", "invite_code": "InviteCode2026"
        })
        self.user_client.post("/api/auth/register", json={
            "phone": "13900000003", "password": "UserPass123", "invite_code": "InviteCode2026"
        })

    def tearDown(self):
        sys.modules.pop("server", None)
        sys.modules.pop("database", None)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def test_reset_code_accepts_whitespace_lowercase_and_display_text(self):
        response = self.admin_client.post("/api/auth/admin/reset-code", json={"phone": "13900000003"})
        self.assertEqual(response.status_code, 200)
        code = response.get_json()["reset_code"]
        pasted = "\u4e00\u6b21\u6027\u91cd\u7f6e\u7801\uff1a" + " - ".join(code.lower()) + "\uff0815 \u5206\u949f\u5185\u6709\u6548\uff09"
        reset = self.user_client.post("/api/auth/reset-password", json={
            "phone": "13900000003", "password": "NewUserPass456", "reset_code": pasted
        })
        self.assertEqual(reset.status_code, 200, reset.get_data(as_text=True))
        login = self.user_client.post("/api/auth/login", json={
            "phone": "13900000003", "password": "NewUserPass456"
        })
        self.assertEqual(login.status_code, 200, login.get_data(as_text=True))

    def test_reset_code_rejects_invalid_format_clearly(self):
        reset = self.user_client.post("/api/auth/reset-password", json={
            "phone": "13900000003", "password": "NewUserPass456", "reset_code": "123"
        })
        self.assertEqual(reset.status_code, 400)
        self.assertIn("10", reset.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
