import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.archive_retired_payee_learning import archive_retired_payee_learning


class RetiredPayeeLearningArchiveScriptTests(TestCase):
    def _make_source_db(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE payee_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_payee TEXT NOT NULL,
                    normalized_payee TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    last_used TEXT
                );
                CREATE TABLE payee_envelope_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    normalized_payee TEXT NOT NULL,
                    envelope_id INTEGER NOT NULL,
                    tx_count INTEGER NOT NULL DEFAULT 0,
                    total_amount_cents INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO payee_aliases(raw_payee, normalized_payee, use_count, last_used)
                VALUES ('RAW PAYEE', 'Clean Payee', 2, '2026-05-17T00:00:00');
                INSERT INTO payee_envelope_stats(account_id, normalized_payee, envelope_id, tx_count, total_amount_cents)
                VALUES (7, 'Clean Payee', 11, 2, 2500);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _table_exists(self, path: Path, table_name: str) -> bool:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone() is not None
        finally:
            conn.close()

    def test_archive_exports_and_drops_retired_predictor_tables(self) -> None:
        with TemporaryDirectory(prefix="payee-learning-archive-test-") as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            user_db = data_dir / "user_dbs" / "test-user.sqlite"
            default_db = data_dir / "data.sqlite"
            archive_root = root / "archive"

            self._make_source_db(default_db)
            self._make_source_db(user_db)
            meta = sqlite3.connect(data_dir / "meta.sqlite")
            try:
                meta.executescript(
                    """
                    CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, db_path TEXT);
                    INSERT INTO users(id, name, db_path) VALUES (1, 'Test User', 'user_dbs/test-user.sqlite');
                    """
                )
                meta.commit()
            finally:
                meta.close()

            manifest = archive_retired_payee_learning(
                data_dir=data_dir,
                archive_root=archive_root,
                timestamp="teststamp",
                drop_active_tables=True,
            )

            self.assertEqual(manifest["totals"], {"payee_aliases": 2, "payee_envelope_stats": 2})
            self.assertEqual(len(manifest["sources"]), 2)
            for source in (default_db, user_db):
                self.assertFalse(self._table_exists(source, "payee_aliases"))
                self.assertFalse(self._table_exists(source, "payee_envelope_stats"))

            archive_db = archive_root / "teststamp" / "payee-learning-archive.sqlite"
            archive = sqlite3.connect(archive_db)
            try:
                self.assertEqual(
                    archive.execute("SELECT COUNT(1) FROM payee_aliases_archive").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    archive.execute("SELECT COUNT(1) FROM payee_envelope_stats_archive").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    archive.execute("SELECT COUNT(1) FROM archive_tables WHERE dropped=1").fetchone()[0],
                    4,
                )
            finally:
                archive.close()
