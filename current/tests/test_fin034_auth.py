import re
import sqlite3
import subprocess
import sys

from app.auth import create_reset_token, hash_password, token_hash, verify_password
from app.db import get_meta_db
from tests.helpers import FinanceAppTestCase


class Fin034AuthTests(FinanceAppTestCase):
    def _default_user(self):
        return get_meta_db().execute(
            "SELECT * FROM users WHERE name='Default' OR LOWER(name)='test user' ORDER BY id LIMIT 1"
        ).fetchone()

    def _set_user_password(self, user_id: int, password: str = "correct horse") -> None:
        meta = get_meta_db()
        meta.execute(
            "UPDATE users SET password_hash=?, password_set_at='2026-06-07T00:00:00' WHERE id=?",
            (hash_password(password), user_id),
        )
        meta.commit()

    def test_meta_schema_migrates_users_to_admin_with_nullable_passwords_and_reset_table(self) -> None:
        meta = get_meta_db()
        columns = {row["name"] for row in meta.execute("PRAGMA table_info(users)")}
        self.assertIn("role", columns)
        self.assertIn("password_hash", columns)
        self.assertIn("password_set_at", columns)
        self.assertIsNotNone(
            meta.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_password_reset_tokens'").fetchone()
        )
        self.assertGreaterEqual(meta.execute("SELECT COUNT(1) AS c FROM users WHERE role='admin'").fetchone()["c"], 1)
        self.assertEqual(meta.execute("SELECT COUNT(1) AS c FROM users WHERE password_hash IS NOT NULL").fetchone()["c"], 0)

    def test_passwordless_user_selection_still_enters_app(self) -> None:
        user = self._default_user()
        response = self.client.post("/users/select", data={"user_id": user["id"]}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("/users/login", response.headers["Location"])
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_passworded_user_requires_login_not_stale_selected_user(self) -> None:
        user = self._default_user()
        self._set_user_password(int(user["id"]))
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(user["id"])
            client_session.pop("auth_user_id", None)

        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/users/login/{user['id']}", response.headers["Location"])

        bad = self.client.post(f"/users/login/{user['id']}", data={"password": "wrong"})
        self.assertEqual(bad.status_code, 403)
        good = self.client.post(f"/users/login/{user['id']}", data={"password": "correct horse"}, follow_redirects=False)
        self.assertEqual(good.status_code, 302)
        self.assertNotIn("/users/login", good.headers["Location"])
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_user_can_set_change_and_remove_optional_password(self) -> None:
        user = self._default_user()
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(user["id"])

        response = self.client.post(
            "/users/settings/password",
            data={"new_password": "first pass", "confirm_password": "first pass"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        row = get_meta_db().execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        self.assertTrue(verify_password(row["password_hash"], "first pass"))

        response = self.client.post(
            "/users/settings/password",
            data={"current_password": "first pass", "new_password": "second pass", "confirm_password": "second pass"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        row = get_meta_db().execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        self.assertTrue(verify_password(row["password_hash"], "second pass"))

        response = self.client.post(
            "/users/settings/password/remove",
            data={"current_password": "second pass"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        row = get_meta_db().execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        self.assertIsNone(row["password_hash"])

    def test_admin_reset_token_is_hash_stored_single_use_and_does_not_clear_password(self) -> None:
        user = self._default_user()
        self._set_user_password(int(user["id"]), "old password")
        token, _ = create_reset_token(int(user["id"]), created_by_admin_user_id=int(user["id"]), ttl_minutes=30)
        token_row = get_meta_db().execute("SELECT token_hash FROM user_password_reset_tokens WHERE user_id=?", (user["id"],)).fetchone()
        self.assertEqual(token_row["token_hash"], token_hash(token))
        self.assertNotIn(token, token_row["token_hash"])
        password_row = get_meta_db().execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        self.assertTrue(verify_password(password_row["password_hash"], "old password"))

        response = self.client.post(
            f"/users/reset/{token}",
            data={"new_password": "new password", "confirm_password": "new password"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        row = get_meta_db().execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        self.assertTrue(verify_password(row["password_hash"], "new password"))
        reused = self.client.get(f"/users/reset/{token}", follow_redirects=False)
        self.assertEqual(reused.status_code, 302)

    def test_admin_routes_require_authenticated_admin_not_just_selected_user_id(self) -> None:
        user = self._default_user()
        self._set_user_password(int(user["id"]))
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(user["id"])
            client_session.pop("auth_user_id", None)
        response = self.client.get("/users/admin", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/users/login/{user['id']}", response.headers["Location"])

    def test_admin_role_guard_blocks_demoting_last_admin(self) -> None:
        user = self._default_user()
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(user["id"])
            client_session["auth_user_id"] = int(user["id"])
        response = self.client.post(
            "/users/admin/role",
            data={"user_id": user["id"], "role": "user"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cannot demote the last Admin", response.get_data(as_text=True))
        role = get_meta_db().execute("SELECT role FROM users WHERE id=?", (user["id"],)).fetchone()["role"]
        self.assertEqual(role, "admin")

    def test_break_glass_script_generates_reset_url_without_storing_plain_token(self) -> None:
        user = self._default_user()
        result = subprocess.run(
            [
                sys.executable,
                "scripts/create_password_reset.py",
                "--user-id",
                str(user["id"]),
                "--ttl-minutes",
                "5",
            ],
            cwd=self.app.config["APP_DIR"].parent,
            env={
                "APP_ENV": "testing",
                "TESTING": "1",
                "SECRET_KEY": "test-secret",
                "APP_DATA_DIR": str(self.app_data_dir),
                "DB_PATH": str(self.app.config["DB_PATH"]),
                "META_DB_PATH": str(self.app.config["META_DB_PATH"]),
                "USER_DB_DIR": str(self.app.config["USER_DB_DIR"]),
                "UPLOAD_DIR": str(self.app.config["UPLOAD_DIR"]),
                "BOOTSTRAP_LEGACY_DATA": "0",
                "REHOME_LEGACY_DB_PATHS": "0",
                "ALLOW_ABSOLUTE_USER_DB_PATHS": "1",
                "FORBID_EXTERNAL_TEST_DB_PATHS": "1",
                "SNAPSHOT_ALERT_ENABLED": "0",
            },
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Created one-time reset", result.stdout)
        match = re.search(r"/users/reset/(\S+)", result.stdout)
        self.assertIsNotNone(match)
        token = match.group(1)
        conn = sqlite3.connect(self.app.config["META_DB_PATH"])
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT token_hash FROM user_password_reset_tokens ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(row["token_hash"], token_hash(token))
            self.assertNotIn(token, row["token_hash"])
        finally:
            conn.close()
