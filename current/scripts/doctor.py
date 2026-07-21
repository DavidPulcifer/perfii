"""Validate a bootstrapped finance-app workspace and run a local app smoke test."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    # Make both `python scripts/doctor.py` and module execution reliable.
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from scripts.bootstrap_workspace import (
    PROJECT_ROOT,
    PUBLIC_PROFILES,
    WORKSPACE_FORMAT_VERSION,
    WORKSPACE_MARKER,
    _is_within,
)
from scripts.ledger_invariants import audit_ledger


ESSENTIAL_LEDGER_TABLES = {
    "accounts",
    "envelopes",
    "savings_plans",
    "savings_rules",
    "savings_transfer_records",
    "schema_migrations",
    "transaction_splits",
    "transactions",
}


def _result(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": passed, "detail": detail}


def _readonly_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _check_sqlite_file(path: Path, *, label: str) -> tuple[list[dict[str, Any]], set[str]]:
    results: list[dict[str, Any]] = []
    tables: set[str] = set()
    if not path.is_file():
        return [_result(label, False, f"Missing database: {path}")], tables

    try:
        conn = _readonly_connection(path)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            results.append(_result(f"{label}.integrity", integrity == "ok", str(integrity)))
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            results.append(
                _result(
                    f"{label}.foreign_keys",
                    not violations,
                    "clean" if not violations else f"{len(violations)} violation(s)",
                )
            )
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        results.append(_result(label, False, f"Could not inspect database: {exc}"))
    return results, tables


def _read_marker(data_dir: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    marker_path = data_dir / WORKSPACE_MARKER
    if not marker_path.is_file():
        return None, [
            _result(
                "workspace.marker",
                False,
                f"Missing {WORKSPACE_MARKER}; refusing to treat this as a managed workspace.",
            )
        ]
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [_result("workspace.marker", False, f"Invalid marker: {exc}")]

    valid = (
        marker.get("format_version") == WORKSPACE_FORMAT_VERSION
        and marker.get("profile") in (*PUBLIC_PROFILES, "test")
    )
    detail = (
        f"format {marker.get('format_version')}, profile {marker.get('profile')}"
        if valid
        else "Unexpected marker format or profile."
    )
    return marker, [_result("workspace.marker", valid, detail)]


def _resolve_registered_db(data_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = data_dir / candidate
    return candidate.resolve()


def _inspect_registered_ledgers(
    data_dir: Path,
    *,
    marker: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[tuple[int, str, Path]]]:
    results: list[dict[str, Any]] = []
    users: list[tuple[int, str, Path]] = []
    meta_path = data_dir / "meta.sqlite"
    meta_results, meta_tables = _check_sqlite_file(meta_path, label="meta")
    results.extend(meta_results)
    if "users" not in meta_tables:
        results.append(_result("meta.users", False, "users table is missing"))
        return results, users

    try:
        conn = _readonly_connection(meta_path)
        try:
            rows = conn.execute("SELECT id, name, db_path FROM users ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        results.append(_result("meta.users", False, f"Could not read users: {exc}"))
        return results, users

    results.append(_result("meta.users", bool(rows), f"{len(rows)} registered user(s)"))
    for row in rows:
        db_path = _resolve_registered_db(data_dir, row["db_path"])
        if not _is_within(db_path, data_dir):
            results.append(
                _result(
                    f"user.{row['id']}.path",
                    False,
                    f"Registered database escapes workspace: {db_path}",
                )
            )
            continue
        results.append(_result(f"user.{row['id']}.path", True, f"{row['name']}: managed path"))
        users.append((int(row["id"]), str(row["name"]), db_path))

    unique_paths = {path for _, _, path in users}
    unique_paths.add((data_dir / "data.sqlite").resolve())
    for index, db_path in enumerate(sorted(unique_paths, key=str), start=1):
        db_results, tables = _check_sqlite_file(db_path, label=f"ledger.{index}")
        results.extend(db_results)
        missing = sorted(ESSENTIAL_LEDGER_TABLES - tables)
        results.append(
            _result(
                f"ledger.{index}.schema",
                not missing,
                "essential tables present" if not missing else f"missing: {', '.join(missing)}",
            )
        )
        if not missing:
            conn = _readonly_connection(db_path)
            try:
                account_count = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
                transaction_count = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
                invariant_results = audit_ledger(conn)
            finally:
                conn.close()
            profile = marker.get("profile")
            if profile in {"demo", "test"}:
                data_valid = account_count >= 5 and transaction_count >= 9
                profile_detail = (
                    f"{account_count} account(s), {transaction_count} transaction(s)"
                )
            else:
                # The schema profile guarantees an empty ledger only at bootstrap.
                # Once the owner begins using that workspace, ordinary valid data
                # must not make doctor or the local launcher reject it.
                data_valid = True
                profile_detail = (
                    f"{account_count} account(s), {transaction_count} transaction(s); "
                    "schema workspace permits user-created data"
                )
            results.append(
                _result(
                    f"ledger.{index}.profile",
                    data_valid,
                    profile_detail,
                )
            )
            results.extend(
                {
                    **item,
                    "check": f"ledger.{index}.{item['check']}",
                }
                for item in invariant_results
            )
    return results, users


def _run_app_smoke(data_dir: Path, *, first_user_id: int) -> list[dict[str, Any]]:
    try:
        from app import create_app
        from app.config import Config

        class DoctorConfig(Config):
            APP_ENV = "testing"
            TESTING = True
            DEBUG = False
            SECRET_KEY = "doctor-local-only"
            HOST = "127.0.0.1"
            PORT = 8091

            APP_DATA_DIR = data_dir
            DB_PATH = data_dir / "data.sqlite"
            META_DB_PATH = data_dir / "meta.sqlite"
            USER_DB_DIR = data_dir / "user_dbs"
            UPLOAD_DIR = data_dir / "uploads"

            TRUST_PROXY_HEADERS = False
            BOOTSTRAP_LEGACY_DATA = False
            REHOME_LEGACY_DB_PATHS = False
            ALLOW_ABSOLUTE_USER_DB_PATHS = False
            FORBID_EXTERNAL_TEST_DB_PATHS = True
            SNAPSHOT_ALERT_ENABLED = False
            SQLITE_JOURNAL_MODE = "DELETE"

        app = create_app(DoctorConfig)
        client = app.test_client()
        users_response = client.get("/users/")
        selected_response = client.post(
            "/users/select",
            data={"user_id": first_user_id},
            follow_redirects=True,
        )
        savings_response = client.get("/savings/")
        return [
            _result(
                "app.users_page",
                users_response.status_code == 200,
                f"HTTP {users_response.status_code}",
            ),
            _result(
                "app.dashboard",
                selected_response.status_code == 200,
                f"HTTP {selected_response.status_code}",
            ),
            _result(
                "app.savings",
                savings_response.status_code == 200,
                f"HTTP {savings_response.status_code}",
            ),
        ]
    except Exception as exc:  # doctor must report import/config/template failures cleanly
        return [_result("app.smoke", False, f"{type(exc).__name__}: {exc}")]


def run_doctor(data_dir: Path, *, smoke: bool = True) -> dict[str, Any]:
    target = data_dir.expanduser().resolve()
    results: list[dict[str, Any]] = []
    if not target.is_dir():
        results.append(_result("workspace.directory", False, f"Missing directory: {target}"))
        return {"ok": False, "data_dir": str(target), "checks": results}
    results.append(_result("workspace.directory", True, "directory exists"))

    marker, marker_results = _read_marker(target)
    results.extend(marker_results)
    if marker is None or not all(item["passed"] for item in marker_results):
        return {"ok": False, "data_dir": str(target), "checks": results}

    for directory in (target / "uploads", target / "user_dbs"):
        results.append(
            _result(
                f"directory.{directory.name}",
                directory.is_dir(),
                "present" if directory.is_dir() else "missing",
            )
        )

    ledger_results, users = _inspect_registered_ledgers(target, marker=marker)
    results.extend(ledger_results)
    if smoke and users and all(item["passed"] for item in results):
        results.extend(_run_app_smoke(target, first_user_id=users[0][0]))
    elif smoke and not users:
        results.append(_result("app.smoke", False, "No safe registered user is available."))

    return {
        "ok": all(item["passed"] for item in results),
        "data_dir": str(target),
        "profile": marker.get("profile"),
        "checks": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check an isolated finance-app workspace.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / ".local" / "demo-data",
        help="Bootstrapped workspace to inspect.",
    )
    parser.add_argument("--no-smoke", action="store_true", help="Skip Flask route checks.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Inspect a verified workspace outside the project directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = args.data_dir.expanduser().resolve()
    if not args.allow_external and not _is_within(target, PROJECT_ROOT):
        report = {
            "ok": False,
            "data_dir": str(target),
            "checks": [
                _result(
                    "workspace.boundary",
                    False,
                    "Refusing to inspect outside the project without --allow-external.",
                )
            ],
        }
    else:
        report = run_doctor(target, smoke=not args.no_smoke)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in report["checks"]:
            status = "PASS" if check["passed"] else "FAIL"
            print(f"[{status}] {check['check']}: {check['detail']}")
        print("Workspace is healthy." if report["ok"] else "Workspace needs attention.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
