from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


APP_ROOT = Path(__file__).resolve().parents[1]


class RetiredSeedDbSafetyTests(unittest.TestCase):
    def test_legacy_seeder_refuses_to_touch_an_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "sentinel.sqlite"
            with closing(sqlite3.connect(database_path)) as connection:
                with connection:
                    connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
                    connection.execute("INSERT INTO sentinel (value) VALUES ('keep me')")

            environment = os.environ.copy()
            environment["DB_PATH"] = str(database_path)
            result = subprocess.run(
                [sys.executable, str(APP_ROOT / "seed_db.py")],
                cwd=APP_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("will not modify any database", result.stderr)
            self.assertIn("bootstrap_workspace.py", result.stderr)
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute("SELECT value FROM sentinel").fetchone()
            self.assertEqual(row, ("keep me",))


if __name__ == "__main__":
    unittest.main()
