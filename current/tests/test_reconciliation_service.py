from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

from app.db import run_schema_migrations, table_columns, table_exists
from app.repositories import reconciliation_repo
from app.services.reconciliation_service import ReconciliationService


class ReconciliationServiceTests(TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'bank',
                acct_key TEXT NOT NULL UNIQUE,
                opening_balance_cents INTEGER NOT NULL DEFAULT 0,
                display_order INTEGER NOT NULL DEFAULT 0,
                CHECK (account_type IN ('bank','credit_card','loan','investment'))
            );
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                ttype TEXT NOT NULL CHECK (ttype IN ('income','expense','transfer_in','transfer_out','allocation')),
                amount_cents INTEGER NOT NULL,
                posted_at TEXT NOT NULL,
                payee TEXT,
                memo TEXT,
                fitid TEXT,
                ignore_match INTEGER NOT NULL DEFAULT 0,
                xfer_pair_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
                external_counterparty TEXT
            );
            """
        )
        run_schema_migrations(self.conn)
        self.conn.executescript(
            """
            INSERT INTO accounts (id, name, account_type, acct_key, opening_balance_cents)
            VALUES
              (1, 'Checking', 'bank', 'acct:checking', 10000),
              (2, 'Savings', 'bank', 'acct:savings', 0),
              (3, 'Visa', 'credit_card', 'acct:visa', 0);
            INSERT INTO transactions (id, account_id, ttype, amount_cents, posted_at, payee, fitid)
            VALUES
              (1, 1, 'income', 50000, '2026-05-01', 'Employer', 'FIT-1'),
              (2, 1, 'expense', -12500, '2026-05-02', 'Grocer', 'FIT-2'),
              (3, 2, 'income', 7000, '2026-05-02', 'Other', 'FIT-3'),
              (4, 3, 'expense', -2000, '2026-05-03', 'Cafe', 'FIT-4'),
              (5, 3, 'transfer_in', 1500, '2026-05-04', 'Payment', 'FIT-5');
            """
        )
        self.conn.commit()
        self.uow_patch = patch("app.services.reconciliation_service.unit_of_work", self._unit_of_work)
        self.uow_patch.start()

    def tearDown(self) -> None:
        self.uow_patch.stop()
        self.conn.close()

    @contextmanager
    def _unit_of_work(self):
        try:
            self.conn.execute("BEGIN")
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def test_migration_is_idempotent_and_creates_reconciliation_tables(self) -> None:
        run_schema_migrations(self.conn)
        run_schema_migrations(self.conn)

        self.assertTrue(table_exists(self.conn, "reconciliation_sessions"))
        self.assertTrue(table_exists(self.conn, "reconciliation_items"))
        self.assertIn("statement_balance_cents", table_columns(self.conn, "reconciliation_sessions"))
        self.assertIn("transaction_id", table_columns(self.conn, "reconciliation_items"))

    def test_bank_formula_create_save_close_and_reopen(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-31",
            statement_balance_cents=47500,
        )
        ReconciliationService.set_cleared_transactions(session_id, [1, 2])

        summary = ReconciliationService.compute(session_id)
        self.assertEqual(summary["starting_balance_cents"], 10000)
        self.assertEqual(summary["selected_total_cents"], 37500)
        self.assertEqual(summary["calculated_balance_cents"], 47500)
        self.assertEqual(summary["difference_cents"], 0)

        ReconciliationService.close_session(session_id)
        self.assertEqual(reconciliation_repo.get_session(session_id, db=self.conn)["status"], "closed")
        self.assertEqual({item["state"] for item in reconciliation_repo.list_items(session_id, db=self.conn)}, {"reconciled"})

        ReconciliationService.reopen_session(session_id)
        self.assertEqual(reconciliation_repo.get_session(session_id, db=self.conn)["status"], "reopened")
        self.assertEqual({item["state"] for item in reconciliation_repo.list_items(session_id, db=self.conn)}, {"cleared"})

    def test_credit_card_formula_uses_signed_statement_balance(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=3,
            statement_date="2026-05-31",
            statement_balance_cents=-500,
        )
        ReconciliationService.set_cleared_transactions(session_id, [4, 5])

        summary = ReconciliationService.compute(session_id)
        self.assertEqual(summary["selected_total_cents"], -500)
        self.assertEqual(summary["calculated_balance_cents"], -500)
        self.assertEqual(summary["difference_cents"], 0)

    def test_close_requires_zero_difference(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-31",
            statement_balance_cents=99999,
        )
        ReconciliationService.set_cleared_transactions(session_id, [1, 2])

        with self.assertRaisesRegex(ValueError, "difference is zero"):
            ReconciliationService.close_session(session_id)
        self.assertEqual(reconciliation_repo.get_session(session_id, db=self.conn)["status"], "open")

    def test_rejects_cross_account_transactions(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-31",
            statement_balance_cents=47500,
        )

        with self.assertRaisesRegex(ValueError, "another account"):
            ReconciliationService.set_cleared_transactions(session_id, [1, 3])
        self.assertEqual(reconciliation_repo.list_items(session_id, db=self.conn), [])

    def test_rejects_duplicate_reconciliation_in_another_closed_session(self) -> None:
        first = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-31",
            statement_balance_cents=47500,
        )
        ReconciliationService.set_cleared_transactions(first, [1, 2])
        ReconciliationService.close_session(first)

        second = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-06-30",
            statement_balance_cents=97500,
        )
        with self.assertRaisesRegex(ValueError, "already reconciled"):
            ReconciliationService.set_cleared_transactions(second, [1])

    def test_transaction_delete_cascades_item_but_preserves_session_history_row(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-31",
            statement_balance_cents=47500,
        )
        ReconciliationService.set_cleared_transactions(session_id, [1, 2])
        ReconciliationService.close_session(session_id)

        self.conn.execute("DELETE FROM transactions WHERE id=?", (2,))
        self.conn.commit()

        self.assertIsNotNone(reconciliation_repo.get_session(session_id, db=self.conn))
        self.assertEqual([item["transaction_id"] for item in reconciliation_repo.list_items(session_id, db=self.conn)], [1])
