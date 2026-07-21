from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.db import get_db, get_meta_db
from app.repositories.import_review_sources_repo import (
    cleanup_expired_import_review_sources,
    create_import_review_source,
    get_import_review_source,
)

from .helpers import FinanceAppTestCase


class ImportReviewSourcesRepoTests(FinanceAppTestCase):
    def _account_id(self) -> int:
        return int(get_db().execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()["id"])

    def test_create_get_scope_expiry_and_cleanup_in_selected_user_db(self) -> None:
        account_id = self._account_id()
        other_account_id = account_id + 999

        source = create_import_review_source(
            account_id=account_id,
            source_bankid=" BANK123 ",
            source_acctid=" ACCT456 ",
            file_hash=" filehash ",
            source_type="qfx",
            source_filename="statement.qfx",
            expires_at="2099-01-01T00:00:00",
        )

        self.assertGreaterEqual(len(source["token"]), 32)
        self.assertNotIn("BANK123", source["token"])
        self.assertEqual(source["source_bankid"], "BANK123")
        self.assertEqual(source["source_acctid"], "ACCT456")
        self.assertEqual(source["file_hash"], "filehash")
        self.assertIsNotNone(get_import_review_source(source["token"], account_id))
        self.assertIsNone(get_import_review_source(source["token"], other_account_id))

        cleanup_expired_import_review_sources(now="2000-01-01T00:00:00")
        self.assertIsNotNone(get_import_review_source(source["token"], account_id))
        cleanup_expired_import_review_sources(now="2100-01-01T00:00:00")
        self.assertIsNone(get_import_review_source(source["token"], account_id))

        user_db = Path(get_meta_db().execute("SELECT db_path FROM users WHERE LOWER(name)=LOWER('test user')").fetchone()["db_path"])
        with closing(sqlite3.connect(self.app_data_dir / "meta.sqlite")) as meta_conn:
            meta_tables = meta_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='import_review_sources'"
            ).fetchall()
        with closing(sqlite3.connect(user_db)) as user_conn:
            user_tables = user_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='import_review_sources'"
            ).fetchall()
        self.assertEqual(meta_tables, [])
        self.assertTrue(user_tables)
