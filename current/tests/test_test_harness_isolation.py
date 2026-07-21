from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from flask import session

from app import create_app
from app.db import close_db, get_db, initialize_empty_db_from_template
from tests.helpers import (
    FinanceAppTestCase,
    build_test_config,
    assert_test_db_paths_isolated,
    prepare_app_data,
)

PRODUCTION_ROOT = Path("/srv/finance-app/data").resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class TestHarnessIsolationTests(FinanceAppTestCase):
    def test_finance_app_test_case_defaults_to_temp_test_user_db(self) -> None:
        db = get_db()
        row = db.execute("PRAGMA database_list").fetchone()
        db_path = Path(row["file"]).resolve()

        self.assertTrue(_is_within(db_path, self.app_data_dir))
        self.assertEqual(db_path, (self.app_data_dir / "user_dbs" / "test-user.sqlite").resolve())
        self.assertFalse(_is_within(db_path, PRODUCTION_ROOT))

    def test_explicit_test_user_selection_uses_temp_test_user_db(self) -> None:
        meta = sqlite3.connect(self.app_data_dir / "meta.sqlite")
        meta.row_factory = sqlite3.Row
        try:
            test_user = meta.execute(
                "SELECT id FROM users WHERE LOWER(name)=LOWER(?)",
                ("test user",),
            ).fetchone()
        finally:
            meta.close()

        self.assertIsNotNone(test_user)
        close_db()
        session["user_id"] = int(test_user["id"])
        db = get_db()
        row = db.execute("PRAGMA database_list").fetchone()
        db_path = Path(row["file"]).resolve()

        self.assertTrue(_is_within(db_path, self.app_data_dir))
        self.assertEqual(db_path, (self.app_data_dir / "user_dbs" / "test-user.sqlite").resolve())
        self.assertFalse(_is_within(db_path, PRODUCTION_ROOT))

    def test_two_selected_users_open_distinct_ledgers(self) -> None:
        first_ledger = (self.app_data_dir / "user_dbs" / "test-user.sqlite").resolve()
        second_ledger = (self.app_data_dir / "user_dbs" / "second-user.sqlite").resolve()
        initialize_empty_db_from_template(second_ledger, first_ledger)

        meta = sqlite3.connect(self.app_data_dir / "meta.sqlite")
        meta.row_factory = sqlite3.Row
        try:
            first_user = meta.execute(
                "SELECT id FROM users WHERE LOWER(name)=LOWER(?)",
                ("test user",),
            ).fetchone()
            second_user_id = meta.execute(
                """
                INSERT INTO users(name, db_path, created_at, role)
                VALUES('Second Synthetic User', ?, '2026-07-20T00:00:00', 'member')
                RETURNING id
                """,
                (str(second_ledger),),
            ).fetchone()["id"]
            meta.commit()
        finally:
            meta.close()

        self.assertIsNotNone(first_user)
        close_db()
        session["user_id"] = int(second_user_id)
        second_db = get_db()
        self.assertEqual(Path(second_db.execute("PRAGMA database_list").fetchone()["file"]).resolve(), second_ledger)
        self.assertEqual(second_db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        second_db.execute(
            "INSERT INTO accounts(name, account_type, acct_key) VALUES(?, 'bank', ?)",
            ("Second Ledger Marker", "synthetic:second-ledger-marker"),
        )
        second_db.commit()

        close_db()
        session["user_id"] = int(first_user["id"])
        first_db = get_db()
        self.assertEqual(Path(first_db.execute("PRAGMA database_list").fetchone()["file"]).resolve(), first_ledger)
        self.assertIsNone(
            first_db.execute(
                "SELECT id FROM accounts WHERE acct_key=?",
                ("synthetic:second-ledger-marker",),
            ).fetchone()
        )


class PrepareAppDataIsolationTests(FinanceAppTestCase):
    def test_prepare_app_data_rewrites_absolute_user_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-app-harness-test-") as raw_temp:
            app_data_dir = prepare_app_data(Path(raw_temp))
            assert_test_db_paths_isolated(app_data_dir)
            conn = sqlite3.connect(app_data_dir / "meta.sqlite")
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT name, db_path FROM users ORDER BY id").fetchall()
            finally:
                conn.close()

            self.assertGreaterEqual(len(rows), 1)
            for row in rows:
                db_path = Path(row["db_path"]).resolve()
                self.assertTrue(_is_within(db_path, app_data_dir))
                self.assertFalse(_is_within(db_path, PRODUCTION_ROOT))

    def test_test_user_meta_path_points_at_generated_user_db(self) -> None:
        conn = sqlite3.connect(self.app_data_dir / "meta.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT db_path FROM users WHERE LOWER(name)=LOWER(?)",
                ("test user",),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(
            Path(row["db_path"]).resolve(),
            (self.app_data_dir / "user_dbs" / "test-user.sqlite").resolve(),
        )


class AppLevelIsolationTripwireTests(FinanceAppTestCase):
    def test_testing_config_refuses_external_test_user_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-app-tripwire-") as raw_temp:
            app_data_dir = prepare_app_data(Path(raw_temp))
            external_path = PRODUCTION_ROOT / "user_dbs" / "test-user.sqlite"
            conn = sqlite3.connect(app_data_dir / "meta.sqlite")
            try:
                conn.execute(
                    "UPDATE users SET db_path=? WHERE LOWER(name)=LOWER(?)",
                    (str(external_path), "test user"),
                )
                conn.commit()
            finally:
                conn.close()

            app = create_app(build_test_config(app_data_dir))
            with app.test_request_context("/"):
                meta = sqlite3.connect(app_data_dir / "meta.sqlite")
                meta.row_factory = sqlite3.Row
                try:
                    test_user = meta.execute(
                        "SELECT id FROM users WHERE LOWER(name)=LOWER(?)",
                        ("test user",),
                    ).fetchone()
                finally:
                    meta.close()

                self.assertIsNotNone(test_user)
                session["user_id"] = int(test_user["id"])
                with self.assertRaisesRegex(RuntimeError, "Refusing to open external test DB path"):
                    get_db()
