from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

from scripts.bootstrap_workspace import initialize_workspace
from scripts.doctor import run_doctor
from scripts.run_local import main as run_local_main


class DoctorSchemaGrowthTests(TestCase):
    def test_schema_workspace_remains_healthy_after_valid_synthetic_use(self) -> None:
        with tempfile.TemporaryDirectory(prefix="finance-schema-growth-") as raw_temp:
            data_dir = initialize_workspace(
                Path(raw_temp) / "workspace",
                profile="schema",
                allow_external=True,
            )
            ledger_path = data_dir / "data.sqlite"
            conn = sqlite3.connect(ledger_path)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                account_id = conn.execute(
                    """
                    INSERT INTO accounts(name, account_type, acct_key, opening_balance_cents)
                    VALUES ('Fictional Checking', 'bank', 'test:schema-growth', 0)
                    """
                ).lastrowid
                envelope_id = conn.execute(
                    """
                    INSERT INTO envelopes(name, locked_account_id, default_amount_cents)
                    VALUES ('Fictional Paycheck', ?, 0)
                    """,
                    (account_id,),
                ).lastrowid
                transaction_id = conn.execute(
                    """
                    INSERT INTO transactions(
                        account_id, ttype, amount_cents, posted_at, payee, memo
                    ) VALUES (?, 'income', 12345, '2026-07-20', ?, ?)
                    """,
                    (
                        account_id,
                        "Example Employer",
                        "Synthetic schema-workspace income",
                    ),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO transaction_splits(transaction_id, envelope_id, amount_cents)
                    VALUES (?, ?, 12345)
                    """,
                    (transaction_id, envelope_id),
                )
                conn.commit()
            finally:
                conn.close()

            report = run_doctor(data_dir, smoke=True)
            self.assertTrue(report["ok"], report)
            profile_checks = [
                item
                for item in report["checks"]
                if item["check"].endswith(".profile")
            ]
            self.assertTrue(profile_checks)
            self.assertTrue(all(item["passed"] for item in profile_checks), profile_checks)
            self.assertTrue(
                all("permits user-created data" in item["detail"] for item in profile_checks),
                profile_checks,
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = run_local_main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--allow-external",
                        "--check-only",
                    ]
                )
            self.assertEqual(exit_code, 0, output.getvalue())
            self.assertIn("Local launch configuration is healthy", output.getvalue())
