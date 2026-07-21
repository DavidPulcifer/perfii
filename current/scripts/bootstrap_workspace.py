"""Create an isolated, new finance-app data workspace.

The bootstrap is intentionally create-only: it will never reuse, reset, or delete
an existing destination.  Public CLI profiles are a schema-only ledger and a
fully fictional demo.  The private ``test`` profile exists so the automated test
suite can build an isolated replacement for the retired snapshot databases.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Final


PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # Direct execution sets sys.path to current/scripts, not current.
    sys.path.insert(0, str(PROJECT_ROOT))
BASE_SCHEMA_PATH: Final = PROJECT_ROOT / "app" / "base_schema.sql"
WORKSPACE_MARKER: Final = ".finance-app-workspace.json"
WORKSPACE_FORMAT_VERSION: Final = 1
FIXTURE_TIMESTAMP: Final = "2026-07-20T00:00:00"
PUBLIC_PROFILES: Final = ("schema", "demo")
ALL_PROFILES: Final = (*PUBLIC_PROFILES, "test")


class BootstrapRefusal(RuntimeError):
    """Raised when a requested bootstrap might affect existing data."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_cli_target(
    data_dir: Path,
    *,
    allow_external: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    """Resolve a CLI destination and enforce conservative path boundaries."""
    target = data_dir.expanduser().resolve()
    root = project_root.resolve()

    if target.exists():
        raise BootstrapRefusal(
            f"Refusing existing destination: {target}. Choose a new, empty path; "
            "this command never resets data."
        )
    if target == root:
        raise BootstrapRefusal("Refusing to use the project root as a data workspace.")
    if not allow_external and not _is_within(target, root):
        raise BootstrapRefusal(
            f"Refusing destination outside the project: {target}. "
            "Pass --allow-external only after verifying the new path."
        )
    return target


def _base_schema_sql(schema_path: Path = BASE_SCHEMA_PATH) -> str:
    try:
        return schema_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"Tracked base schema is missing: {schema_path}") from exc


def _assert_foreign_keys_clean(conn: sqlite3.Connection, *, label: str) -> None:
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"{label} has {len(violations)} foreign-key violation(s).")


def _seed_public_demo_ledger(conn: sqlite3.Connection) -> None:
    """Insert the fixed, institution-neutral demonstration."""
    conn.executemany(
        """
        INSERT INTO accounts(
            id, name, account_type, acct_key, opening_balance_cents,
            opening_date, note, display_order
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "Everyday Checking", "bank", "demo:everyday-checking", 0, "2026-07-01", "Fictional demo account", 1),
            (2, "Quick-Access Savings", "bank", "demo:quick-access-savings", 0, "2026-07-01", "Fictional demo account", 2),
            (3, "High-Yield Savings", "bank", "demo:high-yield-savings", 0, "2026-07-01", "Fictional demo account", 3),
            (4, "Rewards Card", "credit_card", "demo:rewards-card", 0, "2026-07-01", "Fictional demo account", 4),
            (5, "Retirement Investments", "investment", "demo:retirement", 0, "2025-08-01", "Fictional demo account", 5),
            (6, "Education Loan", "loan", "demo:education-loan", 0, "2026-07-01", "Fictional demo account", 6),
        ],
    )
    conn.executemany(
        """
        INSERT INTO envelopes(id, name, locked_account_id, default_amount_cents)
        VALUES (?, ?, ?, ?)
        """,
        [
            (1, "Paycheck Available", 1, 0),
            (2, "Emergency - Quick Access", 2, 0),
            (3, "Home and Car - Quick Access", 2, 0),
            (4, "Future Adventures - Quick Access", 2, 0),
            (5, "Emergency - Long Term", 3, 0),
            (6, "Home and Car - Long Term", 3, 0),
            (7, "Future Adventures - Long Term", 3, 0),
            (8, "Unallocated", 4, 0),
            (9, "Unallocated", 5, 0),
            (10, "Unallocated", 6, 0),
            (11, "Housing", None, 0),
            (12, "Groceries", None, 0),
            (13, "Dining and Fun", None, 0),
        ],
    )
    conn.execute(
        "INSERT INTO credit_cards(account_id, credit_limit_cents) VALUES (?, ?)",
        (4, 600_000),
    )
    conn.execute("INSERT INTO investment_accounts(account_id) VALUES (?)", (5,))
    conn.executemany(
        """
        INSERT INTO investment_valuations(
            id, account_id, asof_date, value_cents, source, note
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (1, 5, "2025-08-01", 2_500_000, "manual", "Fictional opening rollover value"),
            (2, 5, "2025-09-30", 2_590_000, "manual", "Fictional month-end valuation"),
            (3, 5, "2025-10-31", 2_680_000, "manual", "Fictional month-end valuation"),
            (4, 5, "2025-11-30", 2_730_000, "manual", "Fictional month-end valuation"),
            (5, 5, "2025-12-31", 2_870_000, "manual", "Fictional year-end valuation"),
            (6, 5, "2026-01-31", 2_990_000, "manual", "Fictional month-end valuation"),
            (7, 5, "2026-02-28", 3_040_000, "manual", "Fictional month-end valuation"),
            (8, 5, "2026-03-31", 3_210_000, "manual", "Fictional quarter-end valuation"),
            (9, 5, "2026-04-30", 3_120_000, "manual", "Fictional market-pullback valuation"),
            (10, 5, "2026-05-31", 3_340_000, "manual", "Fictional month-end valuation"),
            (11, 5, "2026-06-30", 3_510_000, "manual", "Fictional quarter-end valuation"),
            (12, 5, "2026-07-20", 3_680_000, "manual", "Fictional annual-review valuation"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO investment_notes(
            id, account_id, note_date, body, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                1,
                5,
                "2025-08-01",
                "Opened with a fictional rollover and selected a diversified long-term allocation.",
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
            ),
            (
                2,
                5,
                "2026-01-15",
                "Rebalanced the fictional portfolio back to its target mix.",
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
            ),
            (
                3,
                5,
                "2026-04-10",
                "Kept the monthly contribution steady during a short fictional market pullback.",
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
            ),
            (
                4,
                5,
                "2026-07-20",
                "Annual review: the fictional contribution rate and allocation remain on track.",
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO loans(
            account_id, original_principal_cents, note, normal_monthly_payment_cents
        ) VALUES (?, ?, ?, ?)
        """,
        (6, 1_000_000, "Fictional education loan", 20_000),
    )

    conn.executemany(
        """
        INSERT INTO transactions(
            id, account_id, ttype, amount_cents, posted_at, payee, memo,
            fitid, ignore_match, xfer_pair_id, external_counterparty
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
        """,
        [
            (1, 1, "income", 320_000, "2026-07-15", "Example Employer", "Take-home paycheck", "DEMO-001"),
            (2, 1, "expense", -135_000, "2026-07-16", "Sample Property Management", "Monthly housing", "DEMO-002"),
            (3, 1, "expense", -8_200, "2026-07-17", "Neighborhood Market", "Groceries", "DEMO-003"),
            (4, 2, "income", 200_000, "2026-07-01", "Demo Opening Balance", "Emergency reserve", "DEMO-004"),
            (5, 2, "income", 600_000, "2026-07-01", "Demo Opening Balance", "Home and car reserve", "DEMO-005"),
            (6, 2, "income", 950_000, "2026-07-01", "Demo Opening Balance", "Future adventures reserve", "DEMO-006"),
            (7, 3, "income", 300_000, "2026-07-01", "Demo Opening Balance", "Emergency long-term savings", "DEMO-007"),
            (8, 3, "income", 400_000, "2026-07-01", "Demo Opening Balance", "Home and car long-term savings", "DEMO-008"),
            (9, 3, "income", 200_000, "2026-07-01", "Demo Opening Balance", "Future adventures long-term savings", "DEMO-009"),
            (10, 4, "expense", -2_500, "2026-07-18", "Demo Coffee Shop", "Coffee", "DEMO-010"),
            (11, 4, "expense", -4_200, "2026-07-19", "Example Restaurant", "Dinner", "DEMO-011"),
            (12, 6, "expense", -20_000, "2026-07-10", "Example Loan Servicer", "Monthly payment", "DEMO-012"),
            (13, 6, "expense", -20_000, "2026-07-20", "Example Loan Servicer", "Extra payment", "DEMO-013"),
            (14, 5, "income", 2_500_000, "2025-08-01", "Example Rollover Provider", "Fictional opening rollover contribution", "DEMO-014"),
            (15, 5, "income", 50_000, "2025-09-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-015"),
            (16, 5, "income", 50_000, "2025-10-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-016"),
            (17, 5, "income", 50_000, "2025-11-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-017"),
            (18, 5, "income", 50_000, "2025-12-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-018"),
            (19, 5, "income", 50_000, "2026-01-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-019"),
            (20, 5, "income", 50_000, "2026-02-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-020"),
            (21, 5, "income", 50_000, "2026-03-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-021"),
            (22, 5, "income", 50_000, "2026-04-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-022"),
            (23, 5, "income", 50_000, "2026-05-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-023"),
            (24, 5, "income", 50_000, "2026-06-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-024"),
            (25, 5, "income", 50_000, "2026-07-01", "Example Employer Retirement Plan", "Fictional monthly retirement contribution", "DEMO-025"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO transaction_splits(transaction_id, envelope_id, amount_cents)
        VALUES (?, ?, ?)
        """,
        [
            (1, 1, 320_000),
            (2, 11, -135_000),
            (3, 12, -8_200),
            (4, 2, 200_000),
            (5, 3, 600_000),
            (6, 4, 950_000),
            (7, 5, 300_000),
            (8, 6, 400_000),
            (9, 7, 200_000),
            (10, 13, -2_500),
            (11, 13, -4_200),
            (14, 9, 2_500_000),
            (15, 9, 50_000),
            (16, 9, 50_000),
            (17, 9, 50_000),
            (18, 9, 50_000),
            (19, 9, 50_000),
            (20, 9, 50_000),
            (21, 9, 50_000),
            (22, 9, 50_000),
            (23, 9, 50_000),
            (24, 9, 50_000),
            (25, 9, 50_000),
        ],
    )

    has_savings_planner = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='savings_plans'"
    ).fetchone()
    if has_savings_planner:
        conn.execute(
            """
            INSERT INTO savings_plans(
                id, name, source_account_id, source_envelope_id, created_at, updated_at
            ) VALUES (1, ?, 1, 1, ?, ?)
            """,
            ("Pay Yourself First", FIXTURE_TIMESTAMP, FIXTURE_TIMESTAMP),
        )
        conn.executemany(
            """
            INSERT INTO savings_rules(
                plan_id, name, contribution_basis_points,
                accessible_account_id, accessible_envelope_id,
                long_term_account_id, long_term_envelope_id,
                accessible_target_cents, enabled, display_order,
                created_at, updated_at
            ) VALUES (1, ?, ?, 2, ?, 3, ?, ?, 1, ?, ?, ?)
            """,
            [
                ("Emergency Reserve", 1000, 2, 5, 1_000_000, 1, FIXTURE_TIMESTAMP, FIXTURE_TIMESTAMP),
                ("Home and Car", 500, 3, 6, 1_000_000, 2, FIXTURE_TIMESTAMP, FIXTURE_TIMESTAMP),
                ("Future Adventures", 300, 4, 7, 900_000, 3, FIXTURE_TIMESTAMP, FIXTURE_TIMESTAMP),
            ],
        )


def _seed_legacy_test_ledger(conn: sqlite3.Connection) -> None:
    """Insert the fixed generic baseline expected by legacy regression tests."""
    conn.executemany(
        """
        INSERT INTO accounts(
            id, name, account_type, acct_key, opening_balance_cents,
            opening_date, note, display_order
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "Checking", "bank", "demo:checking", 0, "2026-07-01", "Fictional demo account", 1),
            (2, "Savings", "bank", "demo:savings", 0, "2026-07-01", "Fictional demo account", 2),
            (3, "Visa Card", "credit_card", "demo:card", 0, "2026-07-01", "Fictional demo account", 3),
            (4, "Brokerage", "investment", "demo:brokerage", 0, "2026-07-01", "Fictional demo account", 4),
            (5, "Student Loan", "loan", "demo:loan", 0, "2026-07-01", "Fictional demo account", 5),
        ],
    )
    conn.executemany(
        """
        INSERT INTO envelopes(id, name, locked_account_id, default_amount_cents)
        VALUES (?, ?, ?, ?)
        """,
        [
            (1, "Groceries", None, 0),
            (2, "Dining Out", None, 0),
            (3, "Rent", None, 0),
            (4, "Utilities", None, 0),
            (5, "Travel", None, 0),
            (6, "Emergency Fund", 2, 0),
            (7, "Unallocated", 1, 0),
            (8, "Unallocated", 3, 0),
            (9, "Unallocated", 4, 0),
            (10, "Unallocated", 5, 0),
        ],
    )
    conn.execute(
        "INSERT INTO credit_cards(account_id, credit_limit_cents) VALUES (?, ?)",
        (3, 500_000),
    )
    conn.execute(
        """
        INSERT INTO investment_accounts(account_id) VALUES (?)
        """,
        (4,),
    )
    conn.execute(
        """
        INSERT INTO investment_valuations(
            id, account_id, asof_date, value_cents, source, note
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (1, 4, "2026-07-01", 2_500_000, "manual", "Fictional opening valuation"),
    )
    conn.execute(
        """
        INSERT INTO loans(
            account_id, original_principal_cents, note, normal_monthly_payment_cents
        ) VALUES (?, ?, ?, ?)
        """,
        (5, 1_000_000, "Fictional education loan", 20_000),
    )

    # Transfer links are assigned after both legs exist so foreign keys stay on.
    conn.executemany(
        """
        INSERT INTO transactions(
            id, account_id, ttype, amount_cents, posted_at, payee, memo,
            fitid, ignore_match, xfer_pair_id, external_counterparty
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
        """,
        [
            (1, 1, "income", 300_000, "2026-07-01", "Example Employer", "Paycheck", "DEMO-001"),
            (2, 1, "expense", -150_000, "2026-07-02", "Sample Property Management", "Monthly rent", "DEMO-002"),
            (3, 1, "expense", -4_500, "2026-07-03", "Neighborhood Market", "Groceries", "DEMO-003"),
            (4, 1, "transfer_out", -50_000, "2026-07-05", "Savings", "Pay yourself first", "DEMO-004"),
            (5, 2, "transfer_in", 50_000, "2026-07-05", "Checking", "Pay yourself first", "DEMO-005"),
            (6, 3, "expense", -2_500, "2026-07-06", "Demo Coffee Shop", "Coffee", "DEMO-006"),
            (7, 3, "expense", -4_200, "2026-07-07", "Example Restaurant", "Dinner", "DEMO-007"),
            (8, 5, "expense", -20_000, "2026-07-10", "Example Loan Servicer", "Monthly payment", "DEMO-008"),
            (9, 5, "expense", -20_000, "2026-07-20", "Example Loan Servicer", "Extra payment", "DEMO-009"),
        ],
    )
    conn.execute("UPDATE transactions SET xfer_pair_id=5 WHERE id=4")
    conn.execute("UPDATE transactions SET xfer_pair_id=4 WHERE id=5")
    conn.executemany(
        """
        INSERT INTO transaction_splits(transaction_id, envelope_id, amount_cents)
        VALUES (?, ?, ?)
        """,
        [
            (2, 3, -150_000),
            (3, 1, -4_500),
            (6, 2, -2_500),
            (7, 2, -4_200),
        ],
    )


def _initialize_ledger(path: Path, *, seed_profile: str | None) -> None:
    from app.db import run_schema_migrations

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_base_schema_sql())
        run_schema_migrations(conn)
        if seed_profile == "demo":
            _seed_public_demo_ledger(conn)
        elif seed_profile == "test":
            _seed_legacy_test_ledger(conn)
        _assert_foreign_keys_clean(conn, label=path.name)
        conn.commit()
    finally:
        conn.close()


def _initialize_meta(path: Path, users: list[tuple[str, Path, str]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                db_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                password_hash TEXT,
                password_set_at TEXT
            );
            CREATE TABLE user_password_reset_tokens (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_by_admin_user_id INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(created_by_admin_user_id) REFERENCES users(id)
            );
            CREATE INDEX idx_user_password_reset_tokens_user_id
                ON user_password_reset_tokens(user_id);
            CREATE INDEX idx_user_password_reset_tokens_token_hash
                ON user_password_reset_tokens(token_hash);
            """
        )
        conn.executemany(
            """
            INSERT INTO users(name, db_path, created_at, role)
            VALUES (?, ?, ?, ?)
            """,
            [(name, str(db_path.resolve()), FIXTURE_TIMESTAMP, role) for name, db_path, role in users],
        )
        _assert_foreign_keys_clean(conn, label=path.name)
        conn.commit()
    finally:
        conn.close()


def _build_staging_workspace(staging: Path, final_target: Path, *, profile: str) -> None:
    (staging / "uploads").mkdir(parents=True)
    (staging / "user_dbs").mkdir(parents=True)

    default_db = staging / "data.sqlite"
    _initialize_ledger(default_db, seed_profile=profile if profile in {"demo", "test"} else None)

    if profile == "test":
        test_user_db = staging / "user_dbs" / "test-user.sqlite"
        _initialize_ledger(test_user_db, seed_profile="test")
        users = [("Test User", final_target / "user_dbs" / "test-user.sqlite", "admin")]
    elif profile == "demo":
        users = [("Demo User", final_target / "data.sqlite", "admin")]
    else:
        users = [("Default", final_target / "data.sqlite", "admin")]

    _initialize_meta(staging / "meta.sqlite", users)
    marker = {
        "created_by": "scripts/bootstrap_workspace.py",
        "format_version": WORKSPACE_FORMAT_VERSION,
        "profile": profile,
        "synthetic_data": profile in {"demo", "test"},
    }
    (staging / WORKSPACE_MARKER).write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def initialize_workspace(
    data_dir: Path,
    *,
    profile: str = "demo",
    allow_external: bool = False,
) -> Path:
    """Atomically create a new workspace and refuse every existing destination."""
    if profile not in ALL_PROFILES:
        raise ValueError(f"Unknown workspace profile: {profile!r}")

    target = data_dir.expanduser().resolve()
    if target.exists():
        raise BootstrapRefusal(
            f"Refusing existing destination: {target}. No files were changed."
        )
    if not allow_external and not _is_within(target, PROJECT_ROOT):
        raise BootstrapRefusal(
            f"Refusing destination outside the project: {target}. "
            "External creation requires an explicit allow_external=True."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".finance-bootstrap-", dir=target.parent)).resolve()
    try:
        _build_staging_workspace(staging, target, profile=profile)
        staging.replace(target)
    except Exception:
        # This is the exact staging directory created above, never user data.
        if staging.exists() and staging.name.startswith(".finance-bootstrap-"):
            shutil.rmtree(staging)
        raise
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a new, isolated schema-only or fictional-demo workspace."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / ".local" / "demo-data",
        help="New destination directory (default: .local/demo-data).",
    )
    parser.add_argument(
        "--profile",
        choices=PUBLIC_PROFILES,
        default="demo",
        help="schema creates an empty ledger; demo adds fixed fictional data.",
    )
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Allow a verified new destination outside the project directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target = validate_cli_target(
            args.data_dir,
            allow_external=args.allow_external,
        )
        initialize_workspace(
            target,
            profile=args.profile,
            allow_external=args.allow_external,
        )
    except (BootstrapRefusal, RuntimeError, sqlite3.Error) as exc:
        print(f"Bootstrap refused: {exc}")
        return 2

    print(f"Created {args.profile} workspace at {target}")
    print("No existing database was read, changed, or replaced.")
    print(f"Next: python scripts/doctor.py --data-dir \"{target}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
