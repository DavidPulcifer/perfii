from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

from app.db import run_schema_migrations
from scripts.bootstrap_workspace import initialize_workspace
from scripts.doctor import run_doctor
from scripts.ledger_invariants import audit_ledger


APP_ROOT = Path(__file__).resolve().parents[1]


class LedgerInvariantTests(TestCase):
    def _healthy_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript((APP_ROOT / "app" / "base_schema.sql").read_text(encoding="utf-8"))
        run_schema_migrations(conn)

        conn.executemany(
            """
            INSERT INTO accounts(id, name, account_type, acct_key)
            VALUES (?, ?, 'bank', ?)
            """,
            [
                (1, "Fictional Checking", "test:checking"),
                (2, "Fictional Accessible Savings", "test:accessible"),
                (3, "Fictional Long-Term Savings", "test:long-term"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO envelopes(id, name, locked_account_id)
            VALUES (?, ?, ?)
            """,
            [
                (1, "Fictional Paycheck", 1),
                (2, "Fictional Reserve", 2),
                (3, "Fictional Future", 3),
                (4, "Fictional Flexible", None),
            ],
        )
        conn.executemany(
            """
            INSERT INTO transactions(
                id, account_id, ttype, amount_cents, posted_at,
                payee, memo, xfer_pair_id, external_counterparty
            ) VALUES (?, ?, ?, ?, '2026-07-20', ?, ?, ?, ?)
            """,
            [
                (1, 1, "income", 10_000, "Example Employer", "Fictional income", None, None),
                (2, 1, "transfer_out", -2_000, None, "Fictional transfer", None, None),
                (3, 2, "transfer_in", 2_000, None, "Fictional transfer", None, None),
                # A transaction without splits can be intentionally unallocated.
                (4, 1, "expense", -500, "Example Shop", "Fictional unallocated", None, None),
                # An explicitly external transfer is not expected to have a local pair.
                (5, 1, "transfer_out", -700, None, "Fictional external transfer", None, "Outside Bank"),
            ],
        )
        conn.execute("UPDATE transactions SET xfer_pair_id = 3 WHERE id = 2")
        conn.execute("UPDATE transactions SET xfer_pair_id = 2 WHERE id = 3")
        conn.executemany(
            """
            INSERT INTO transaction_splits(transaction_id, envelope_id, amount_cents)
            VALUES (?, ?, ?)
            """,
            [(1, 1, 10_000), (2, 1, -2_000), (3, 2, 2_000)],
        )
        conn.execute(
            """
            INSERT INTO savings_plans(
                id, name, source_account_id, source_envelope_id, created_at, updated_at
            ) VALUES (1, 'Fictional Plan', 1, 1, '2026-07-20', '2026-07-20')
            """
        )
        conn.execute(
            """
            INSERT INTO savings_rules(
                id, plan_id, name, contribution_basis_points,
                accessible_account_id, accessible_envelope_id,
                long_term_account_id, long_term_envelope_id,
                accessible_target_cents, enabled, display_order, created_at, updated_at
            ) VALUES (
                1, 1, 'Fictional Goal', 4000, 2, 2, 3, 3,
                100000, 1, 1, '2026-07-20', '2026-07-20'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO savings_transfer_records(
                idempotency_key, plan_id, group_index, tx_out_id, tx_in_id, created_at
            ) VALUES ('fictional-record', 1, 0, 2, 3, '2026-07-20')
            """
        )
        conn.execute(
            """
            INSERT INTO reconciliation_sessions(
                id, account_id, statement_date, statement_balance_cents,
                starting_balance_cents, status, created_at, updated_at
            ) VALUES (1, 1, '2026-07-20', 0, 0, 'open', '2026-07-20', '2026-07-20')
            """
        )
        conn.execute(
            """
            INSERT INTO reconciliation_items(
                id, session_id, transaction_id, state, created_at, updated_at
            ) VALUES (1, 1, 2, 'cleared', '2026-07-20', '2026-07-20')
            """
        )
        conn.commit()
        return conn

    @staticmethod
    def _checks(conn: sqlite3.Connection) -> dict[str, dict]:
        return {item["check"]: item for item in audit_ledger(conn)}

    def _assert_corruption_fails(self, sql: str, expected_check: str) -> None:
        conn = self._healthy_connection()
        try:
            conn.execute(sql)
            conn.commit()
            checks = self._checks(conn)
            self.assertFalse(checks[expected_check]["passed"], checks)
            self.assertGreater(checks[expected_check]["violations"], 0)
        finally:
            conn.close()

    def test_healthy_synthetic_ledger_passes_without_writes(self) -> None:
        conn = self._healthy_connection()
        try:
            changes_before = conn.total_changes
            checks = self._checks(conn)

            self.assertEqual(
                set(checks),
                {
                    "invariant.transaction_split_totals",
                    "invariant.transfer_pairs",
                    "invariant.locked_envelope_splits",
                    "invariant.savings_transfer_records",
                    "invariant.reconciliation_accounts",
                    "invariant.savings_enabled_percentage",
                    "invariant.savings_rule_locks",
                },
            )
            self.assertTrue(all(item["passed"] for item in checks.values()), checks)
            self.assertEqual(conn.total_changes, changes_before)
            self.assertTrue(all(item["detail"] == "clean" for item in checks.values()))
        finally:
            conn.close()

    def test_detects_split_total_mismatch(self) -> None:
        self._assert_corruption_fails(
            "UPDATE transaction_splits SET amount_cents = amount_cents + 1 WHERE transaction_id = 1",
            "invariant.transaction_split_totals",
        )

    def test_detects_invalid_transfer_pair(self) -> None:
        self._assert_corruption_fails(
            "UPDATE transactions SET xfer_pair_id = id WHERE id = 3",
            "invariant.transfer_pairs",
        )

    def test_detects_same_account_transfer_pair(self) -> None:
        conn = self._healthy_connection()
        try:
            conn.execute(
                "UPDATE transaction_splits SET envelope_id = 4 WHERE transaction_id IN (2, 3)"
            )
            conn.execute("UPDATE transactions SET account_id = 1 WHERE id = 3")
            conn.commit()

            checks = self._checks(conn)
            self.assertFalse(checks["invariant.transfer_pairs"]["passed"], checks)
            self.assertGreater(checks["invariant.transfer_pairs"]["violations"], 0)
            self.assertTrue(checks["invariant.locked_envelope_splits"]["passed"], checks)
        finally:
            conn.close()

    def test_detects_internal_unpaired_transfer_but_allows_external_one(self) -> None:
        conn = self._healthy_connection()
        try:
            self.assertTrue(self._checks(conn)["invariant.transfer_pairs"]["passed"])
            conn.execute(
                """
                INSERT INTO transactions(
                    account_id, ttype, amount_cents, posted_at, memo, external_counterparty
                ) VALUES (1, 'transfer_out', -100, '2026-07-20', 'Fictional internal transfer', NULL)
                """
            )
            conn.commit()
            self.assertFalse(self._checks(conn)["invariant.transfer_pairs"]["passed"])
        finally:
            conn.close()

    def test_detects_locked_envelope_account_mismatch(self) -> None:
        self._assert_corruption_fails(
            "UPDATE transaction_splits SET envelope_id = 1 WHERE transaction_id = 3",
            "invariant.locked_envelope_splits",
        )

    def test_detects_invalid_savings_transfer_record(self) -> None:
        self._assert_corruption_fails(
            "UPDATE savings_transfer_records SET tx_out_id = 1",
            "invariant.savings_transfer_records",
        )

    def test_detects_reconciliation_account_mismatch(self) -> None:
        self._assert_corruption_fails(
            "UPDATE reconciliation_items SET transaction_id = 3 WHERE id = 1",
            "invariant.reconciliation_accounts",
        )

    def test_detects_enabled_savings_percentage_over_one_hundred(self) -> None:
        self._assert_corruption_fails(
            """
            INSERT INTO savings_rules(
                plan_id, name, contribution_basis_points,
                accessible_account_id, accessible_envelope_id,
                accessible_target_cents, enabled, display_order, created_at, updated_at
            ) VALUES (
                1, 'Fictional Extra Goal', 7000, 2, 2, 0, 1, 2,
                '2026-07-20', '2026-07-20'
            )
            """,
            "invariant.savings_enabled_percentage",
        )

    def test_detects_savings_rule_envelope_lock_mismatch(self) -> None:
        self._assert_corruption_fails(
            "UPDATE savings_rules SET accessible_envelope_id = 3 WHERE id = 1",
            "invariant.savings_rule_locks",
        )

    def test_managed_workspace_doctor_includes_invariants(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-invariant-doctor-") as raw_temp:
            data_dir = initialize_workspace(
                Path(raw_temp) / "workspace",
                profile="demo",
                allow_external=True,
            )

            healthy_report = run_doctor(data_dir, smoke=False)
            invariant_checks = [
                item for item in healthy_report["checks"] if ".invariant." in item["check"]
            ]
            self.assertTrue(healthy_report["ok"], healthy_report)
            self.assertEqual(len(invariant_checks), 7)
            self.assertTrue(all(item["passed"] for item in invariant_checks))

            conn = sqlite3.connect(data_dir / "data.sqlite")
            try:
                conn.execute(
                    """
                    UPDATE transaction_splits
                    SET amount_cents = amount_cents + 1
                    WHERE id = (SELECT MIN(id) FROM transaction_splits)
                    """
                )
                conn.commit()
            finally:
                conn.close()

            corrupt_report = run_doctor(data_dir, smoke=False)
            failed_names = {
                item["check"]
                for item in corrupt_report["checks"]
                if not item["passed"]
            }
            self.assertFalse(corrupt_report["ok"])
            self.assertIn(
                "ledger.1.invariant.transaction_split_totals",
                failed_names,
            )

    def test_managed_workspace_doctor_detects_same_account_transfer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-invariant-doctor-") as raw_temp:
            data_dir = initialize_workspace(
                Path(raw_temp) / "workspace",
                profile="test",
                allow_external=True,
            )
            conn = sqlite3.connect(data_dir / "data.sqlite")
            try:
                pair = conn.execute(
                    """
                    SELECT t.id, t.xfer_pair_id, t.account_id
                    FROM transactions AS t
                    JOIN transactions AS pair ON pair.id = t.xfer_pair_id
                    WHERE t.ttype = 'transfer_out'
                    ORDER BY t.id
                    LIMIT 1
                    """
                ).fetchone()
                self.assertIsNotNone(pair)
                conn.execute(
                    "UPDATE transactions SET account_id=? WHERE id=?",
                    (pair[2], pair[1]),
                )
                conn.commit()
            finally:
                conn.close()

            report = run_doctor(data_dir, smoke=False)
            failed_names = {
                item["check"]
                for item in report["checks"]
                if not item["passed"]
            }
            self.assertFalse(report["ok"])
            self.assertIn("ledger.1.invariant.transfer_pairs", failed_names)
