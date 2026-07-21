from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

from app.services.savings_planner_service import long_term_share_basis_points
from scripts.bootstrap_workspace import (
    BootstrapRefusal,
    initialize_workspace,
    validate_cli_target,
)
from scripts.doctor import run_doctor


class WorkspaceBootstrapTests(TestCase):
    def test_schema_profile_creates_empty_current_ledger(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-schema-bootstrap-") as raw_temp:
            data_dir = Path(raw_temp) / "workspace"
            initialize_workspace(data_dir, profile="schema", allow_external=True)

            conn = sqlite3.connect(data_dir / "data.sqlite")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                        ("20260720_01_savings_planner_schema",),
                    ).fetchone()[0],
                    1,
                )
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='savings_plans'"
                    ).fetchone()
                )
            finally:
                conn.close()

            report = run_doctor(data_dir, smoke=True)
            self.assertTrue(report["ok"], report)

    def test_demo_profile_is_fictional_deterministic_and_savings_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-demo-bootstrap-") as raw_temp:
            root = Path(raw_temp)
            first = initialize_workspace(root / "first", profile="demo", allow_external=True)
            second = initialize_workspace(root / "second", profile="demo", allow_external=True)

            snapshots = []
            for data_dir in (first, second):
                conn = sqlite3.connect(data_dir / "data.sqlite")
                try:
                    accounts = conn.execute(
                        "SELECT id, name, account_type FROM accounts ORDER BY id"
                    ).fetchall()
                    rules = conn.execute(
                        """
                        SELECT name, contribution_basis_points,
                               accessible_account_id, accessible_envelope_id,
                               long_term_account_id, long_term_envelope_id,
                               accessible_target_cents
                        FROM savings_rules ORDER BY display_order, id
                        """
                    ).fetchall()
                    balances = conn.execute(
                        """
                        SELECT e.name, COALESCE(SUM(s.amount_cents), 0)
                        FROM envelopes e
                        LEFT JOIN transaction_splits s ON s.envelope_id=e.id
                        WHERE e.id IN (2, 3, 4)
                        GROUP BY e.id ORDER BY e.id
                        """
                    ).fetchall()
                    investment_contributions = conn.execute(
                        """
                        SELECT posted_at, ttype, amount_cents, payee, memo
                        FROM transactions
                        WHERE account_id=5
                        ORDER BY posted_at, id
                        """
                    ).fetchall()
                    investment_valuations = conn.execute(
                        """
                        SELECT asof_date, value_cents, note
                        FROM investment_valuations
                        WHERE account_id=5
                        ORDER BY asof_date, id
                        """
                    ).fetchall()
                    investment_notes = conn.execute(
                        """
                        SELECT note_date, body
                        FROM investment_notes
                        WHERE account_id=5
                        ORDER BY note_date, id
                        """
                    ).fetchall()
                    contribution_split_totals = conn.execute(
                        """
                        SELECT t.id, t.amount_cents, COALESCE(SUM(s.amount_cents), 0)
                        FROM transactions t
                        LEFT JOIN transaction_splits s ON s.transaction_id=t.id
                        WHERE t.account_id=5
                        GROUP BY t.id
                        ORDER BY t.posted_at, t.id
                        """
                    ).fetchall()
                    snapshots.append(
                        (
                            accounts,
                            rules,
                            balances,
                            investment_contributions,
                            investment_valuations,
                            investment_notes,
                            contribution_split_totals,
                        )
                    )
                finally:
                    conn.close()

            self.assertEqual(snapshots[0], snapshots[1])
            account_names = [row[1] for row in snapshots[0][0]]
            self.assertEqual(
                account_names[:4],
                [
                    "Everyday Checking",
                    "Quick-Access Savings",
                    "High-Yield Savings",
                    "Rewards Card",
                ],
            )
            self.assertEqual([row[0] for row in snapshots[0][1]], [
                "Emergency Reserve",
                "Home and Car",
                "Future Adventures",
            ])
            accessible_balances = [row[1] for row in snapshots[0][2]]
            self.assertEqual(accessible_balances, [200_000, 600_000, 950_000])
            self.assertEqual(
                [
                    long_term_share_basis_points(
                        balance,
                        rule[6],
                        has_long_term_destination=True,
                    )
                    for balance, rule in zip(accessible_balances, snapshots[0][1])
                ],
                [0, 0, 10_000],
            )
            contributions = snapshots[0][3]
            self.assertEqual(len(contributions), 12)
            self.assertEqual(contributions[0][:3], ("2025-08-01", "income", 2_500_000))
            self.assertEqual(contributions[-1][:3], ("2026-07-01", "income", 50_000))
            self.assertEqual(sum(row[2] for row in contributions), 3_050_000)
            self.assertTrue(all(row[4].startswith("Fictional") for row in contributions))

            valuations = snapshots[0][4]
            self.assertEqual(len(valuations), 12)
            self.assertEqual(valuations[0][:2], ("2025-08-01", 2_500_000))
            self.assertEqual(valuations[-1][:2], ("2026-07-20", 3_680_000))
            self.assertEqual(valuations[-1][1] - sum(row[2] for row in contributions), 630_000)
            self.assertLess(valuations[8][1], valuations[7][1])

            notes = snapshots[0][5]
            self.assertEqual(len(notes), 4)
            self.assertEqual(notes[0][0], "2025-08-01")
            self.assertIn("fictional", notes[0][1].lower())
            self.assertEqual(notes[-1][0], "2026-07-20")
            self.assertIn("fictional", notes[-1][1].lower())

            split_totals = snapshots[0][6]
            self.assertTrue(all(amount == split_total for _, amount, split_total in split_totals))

            report = run_doctor(first, smoke=True)
            self.assertTrue(report["ok"], report)

    def test_existing_destination_is_never_reused_or_reset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-bootstrap-refusal-") as raw_temp:
            target = Path(raw_temp) / "existing"
            target.mkdir()
            sentinel = target / "keep-me.txt"
            sentinel.write_text("unchanged", encoding="utf-8")

            with self.assertRaises(BootstrapRefusal):
                initialize_workspace(target, profile="demo")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(list(target.iterdir()), [sentinel])

    def test_cli_requires_explicit_opt_in_for_external_new_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-bootstrap-boundary-") as raw_temp:
            external = Path(raw_temp) / "new-workspace"
            with self.assertRaises(BootstrapRefusal):
                validate_cli_target(external)
            with self.assertRaises(BootstrapRefusal):
                initialize_workspace(external, profile="schema")
            self.assertEqual(
                validate_cli_target(external, allow_external=True),
                external.resolve(),
            )

    def test_doctor_rejects_registered_database_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-doctor-boundary-") as raw_temp:
            root = Path(raw_temp)
            data_dir = initialize_workspace(root / "workspace", profile="demo", allow_external=True)
            external = root / "external.sqlite"
            external.write_bytes(b"not a ledger")
            conn = sqlite3.connect(data_dir / "meta.sqlite")
            try:
                conn.execute("UPDATE users SET db_path=?", (str(external.resolve()),))
                conn.commit()
            finally:
                conn.close()

            report = run_doctor(data_dir, smoke=False)

            self.assertFalse(report["ok"])
            self.assertTrue(
                any(
                    check["check"] == "user.1.path" and not check["passed"]
                    for check in report["checks"]
                )
            )
            self.assertEqual(external.read_bytes(), b"not a ledger")
