from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Type
from unittest import TestCase

from flask import session

from app import create_app
from app.config import Config
from app.db import get_meta_db
from scripts.bootstrap_workspace import initialize_workspace

def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False

def assert_test_db_paths_isolated(app_data_dir: Path) -> None:
    """Fail if generated test metadata can open a DB outside app_data_dir."""
    root = app_data_dir.resolve()
    meta_path = root / "meta.sqlite"
    conn = sqlite3.connect(meta_path)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT id, name, db_path FROM users ORDER BY id"):
            user_id = row["id"]
            user_name = row["name"]
            db_path = Path(row["db_path"]).expanduser().resolve()
            if not _path_within(db_path, root):
                raise AssertionError(
                    f"Test user {user_id} ({user_name!r}) points outside temp app-data: {db_path}"
                )
            if not db_path.exists():
                raise AssertionError(
                    f"Test user {user_id} ({user_name!r}) points at missing DB: {db_path}"
                )
    finally:
        conn.close()


def prepare_app_data(temp_dir: Path) -> Path:
    """Generate a fresh, fictional fixture without reading a snapshot database."""
    app_data_dir = temp_dir / "app-data"
    initialize_workspace(app_data_dir, profile="test", allow_external=True)
    assert_test_db_paths_isolated(app_data_dir)
    return app_data_dir


def build_test_config(app_data_dir: Path) -> Type[Config]:
    class TestConfig(Config):
        APP_ENV = "testing"
        TESTING = True
        DEBUG = False
        SECRET_KEY = "test-secret"
        HOST = "127.0.0.1"
        PORT = 8091

        APP_DATA_DIR = app_data_dir
        DB_PATH = app_data_dir / "data.sqlite"
        META_DB_PATH = app_data_dir / "meta.sqlite"
        USER_DB_DIR = app_data_dir / "user_dbs"
        UPLOAD_DIR = app_data_dir / "uploads"

        TRUST_PROXY_HEADERS = False
        BOOTSTRAP_LEGACY_DATA = False
        REHOME_LEGACY_DB_PATHS = True
        ALLOW_ABSOLUTE_USER_DB_PATHS = True
        FORBID_EXTERNAL_TEST_DB_PATHS = True
        SNAPSHOT_ALERT_ENABLED = False

    return TestConfig


class FinanceAppTestCase(TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="finance-app-tests-")
        self.temp_path = Path(self._tempdir.name)
        self.app_data_dir = prepare_app_data(self.temp_path)
        self.app = create_app(build_test_config(self.app_data_dir))
        self.app_context = self.app.app_context()
        self.app_context.push()

        self.request_context = self.app.test_request_context("/")
        self.request_context.push()
        self._select_default_test_user()

        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.request_context.pop()
        self.app_context.pop()
        self._tempdir.cleanup()

    def _select_default_test_user(self) -> None:
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if row is not None:
            session["user_id"] = int(row["id"])
