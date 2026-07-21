#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import assert_test_db_paths_isolated, prepare_app_data


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="finance-app-isolation-") as raw_temp:
        app_data_dir = prepare_app_data(Path(raw_temp))
        assert_test_db_paths_isolated(app_data_dir)
        conn = sqlite3.connect(app_data_dir / "meta.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            print(f"PASS: test DB paths are isolated under {app_data_dir}")
            for row in conn.execute("SELECT id, name, db_path FROM users ORDER BY id"):
                print(f"  user {row['id']} {row['name']}: {row['db_path']}")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
