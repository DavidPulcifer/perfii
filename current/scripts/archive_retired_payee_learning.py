#!/usr/bin/env python3
"""Archive and remove FIN-047 retired payee-learning predictor tables.

This is a one-time operational script. It exports the retired legacy predictor
state into a private archive SQLite database, then optionally drops the old
active tables so they cannot be mistaken for live predictor data.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

LEGACY_TABLES = ("payee_aliases", "payee_envelope_stats")


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def sqlite_connect_existing(path: Path, *, writable: bool) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def active_database_paths(data_dir: Path) -> list[Path]:
    """Return active app/user SQLite DBs that may contain retired predictor data."""
    data_dir = data_dir.resolve()
    paths: list[Path] = []

    def add(path: Path) -> None:
        path = path.expanduser()
        if not path.is_absolute():
            path = data_dir / path
        path = path.resolve()
        if path.exists() and path.suffix == ".sqlite" and path not in paths:
            paths.append(path)

    add(data_dir / "data.sqlite")

    meta_path = data_dir / "meta.sqlite"
    if meta_path.exists():
        try:
            with closing(sqlite_connect_existing(meta_path, writable=False)) as meta:
                if table_exists(meta, "users"):
                    for row in meta.execute("SELECT db_path FROM users WHERE db_path IS NOT NULL"):
                        add(Path(str(row["db_path"])))
        except sqlite3.Error:
            # Let the caller still archive the default/user_dbs candidates.
            pass

    user_dir = data_dir / "user_dbs"
    if user_dir.exists():
        for path in sorted(user_dir.glob("*.sqlite")):
            add(path)

    return paths


def ensure_archive_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS archive_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            archived_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_tables (
            source_id INTEGER NOT NULL,
            table_name TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            dropped INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (source_id, table_name),
            FOREIGN KEY(source_id) REFERENCES archive_sources(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS payee_aliases_archive (
            source_id INTEGER NOT NULL,
            id INTEGER,
            raw_payee TEXT,
            normalized_payee TEXT,
            use_count INTEGER,
            last_used TEXT,
            FOREIGN KEY(source_id) REFERENCES archive_sources(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS payee_envelope_stats_archive (
            source_id INTEGER NOT NULL,
            id INTEGER,
            account_id INTEGER,
            normalized_payee TEXT,
            envelope_id INTEGER,
            tx_count INTEGER,
            total_amount_cents INTEGER,
            FOREIGN KEY(source_id) REFERENCES archive_sources(id) ON DELETE CASCADE
        );
        """
    )


def copy_payee_aliases(src: sqlite3.Connection, archive: sqlite3.Connection, source_id: int) -> int:
    rows = [dict(row) for row in src.execute("SELECT * FROM payee_aliases")]
    archive.executemany(
        """
        INSERT INTO payee_aliases_archive (
            source_id, id, raw_payee, normalized_payee, use_count, last_used
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_id,
                row.get("id"),
                row.get("raw_payee"),
                row.get("normalized_payee"),
                row.get("use_count"),
                row.get("last_used"),
            )
            for row in rows
        ],
    )
    return len(rows)


def copy_payee_envelope_stats(src: sqlite3.Connection, archive: sqlite3.Connection, source_id: int) -> int:
    rows = [dict(row) for row in src.execute("SELECT * FROM payee_envelope_stats")]
    archive.executemany(
        """
        INSERT INTO payee_envelope_stats_archive (
            source_id, id, account_id, normalized_payee, envelope_id, tx_count, total_amount_cents
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_id,
                row.get("id"),
                row.get("account_id"),
                row.get("normalized_payee"),
                row.get("envelope_id"),
                row.get("tx_count"),
                row.get("total_amount_cents"),
            )
            for row in rows
        ],
    )
    return len(rows)


def archive_one_database(
    source_path: Path,
    archive_conn: sqlite3.Connection,
    *,
    drop_active_tables: bool,
    archived_at: str,
) -> dict[str, Any] | None:
    with closing(sqlite_connect_existing(source_path, writable=drop_active_tables)) as src, src:
        present = [table for table in LEGACY_TABLES if table_exists(src, table)]
        if not present:
            return None

        source_id = int(
            archive_conn.execute(
                "INSERT INTO archive_sources(source_path, archived_at) VALUES(?, ?)",
                (str(source_path), archived_at),
            ).lastrowid
        )
        table_counts: dict[str, int] = {}

        if "payee_aliases" in present:
            table_counts["payee_aliases"] = copy_payee_aliases(src, archive_conn, source_id)
        if "payee_envelope_stats" in present:
            table_counts["payee_envelope_stats"] = copy_payee_envelope_stats(src, archive_conn, source_id)

        for table in present:
            archive_conn.execute(
                """
                INSERT INTO archive_tables(source_id, table_name, row_count, dropped)
                VALUES (?, ?, ?, ?)
                """,
                (source_id, table, int(table_counts.get(table, 0)), 1 if drop_active_tables else 0),
            )

        archive_conn.commit()

        if drop_active_tables:
            src.execute("DROP TABLE IF EXISTS payee_aliases")
            src.execute("DROP TABLE IF EXISTS payee_envelope_stats")
            src.commit()

        return {
            "source_path": str(source_path),
            "tables": table_counts,
            "dropped": bool(drop_active_tables),
        }


def archive_retired_payee_learning(
    *,
    data_dir: Path,
    archive_root: Path | None = None,
    drop_active_tables: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    timestamp = timestamp or utc_timestamp()
    archive_root = (archive_root or data_dir / "archive" / "FIN047-payee-learning").resolve()
    archive_dir = archive_root / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_db = archive_dir / "payee-learning-archive.sqlite"
    manifest_path = archive_dir / "manifest.json"

    source_paths = active_database_paths(data_dir)
    archived_at = datetime.now(UTC).isoformat(timespec="seconds")

    sources = []
    with closing(sqlite3.connect(archive_db)) as archive_conn:
        with archive_conn:
            archive_conn.row_factory = sqlite3.Row
            ensure_archive_schema(archive_conn)
            for source_path in source_paths:
                result = archive_one_database(
                    source_path,
                    archive_conn,
                    drop_active_tables=drop_active_tables,
                    archived_at=archived_at,
                )
                if result:
                    sources.append(result)

    manifest = {
        "timestamp": timestamp,
        "archived_at": archived_at,
        "data_dir": str(data_dir),
        "archive_dir": str(archive_dir),
        "archive_db": str(archive_db),
        "drop_active_tables": bool(drop_active_tables),
        "sources": sources,
        "totals": {
            table: sum(int(source["tables"].get(table, 0)) for source in sources)
            for table in LEGACY_TABLES
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Explicit Finance App runtime data directory. No production path is assumed.",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help="Root directory for timestamped archive output. Defaults under data/archive/FIN047-payee-learning.",
    )
    parser.add_argument("--timestamp", default=None, help="Archive timestamp override for repeatable tests.")
    parser.add_argument("--drop-active-tables", action="store_true", help="Drop retired tables after export.")
    parser.add_argument("--yes", action="store_true", help="Required with --drop-active-tables.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.drop_active_tables and not args.yes:
        raise SystemExit("Refusing to drop active tables without --yes")

    manifest = archive_retired_payee_learning(
        data_dir=args.data_dir,
        archive_root=args.archive_root,
        drop_active_tables=args.drop_active_tables,
        timestamp=args.timestamp,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
