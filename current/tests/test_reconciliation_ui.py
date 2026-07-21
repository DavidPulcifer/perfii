from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

from app import create_app
from app.config import Config
from app.db import get_db, get_meta_db, run_schema_migrations
from app.repositories import reconciliation_repo
from app.services.reconciliation_service import ReconciliationService


def _config(app_data_dir: Path):
    class TestConfig(Config):
        APP_ENV = "testing"
        TESTING = True
        SECRET_KEY = "test-secret"
        HOST = "127.0.0.1"
        APP_DATA_DIR = app_data_dir
        DB_PATH = app_data_dir / "data.sqlite"
        META_DB_PATH = app_data_dir / "meta.sqlite"
        USER_DB_DIR = app_data_dir / "user_dbs"
        UPLOAD_DIR = app_data_dir / "uploads"
        BOOTSTRAP_LEGACY_DATA = False
        REHOME_LEGACY_DB_PATHS = False
        SNAPSHOT_ALERT_ENABLED = False

    return TestConfig


class ReconciliationUiTests(TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="fin031-ui-")
        self.app_data_dir = Path(self.tempdir.name)
        self.app = create_app(_config(self.app_data_dir))
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._seed_db()
        self.client = self.app.test_client()
        self._select_user()

    def tearDown(self) -> None:
        self.ctx.pop()
        self.tempdir.cleanup()

    def _select_user(self) -> None:
        row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        with self.client.session_transaction() as sess:
            sess["user_id"] = int(row["id"])

    def _seed_db(self) -> None:
        conn = sqlite3.connect(self.app.config["DB_PATH"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'bank',
                acct_key TEXT NOT NULL UNIQUE,
                opening_balance_cents INTEGER NOT NULL DEFAULT 0,
                opening_date TEXT,
                bankid TEXT,
                acctid TEXT,
                display_order INTEGER NOT NULL DEFAULT 0,
                CHECK (account_type IN ('bank','credit_card','loan','investment'))
            );
            CREATE TABLE envelopes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                locked_account_id INTEGER,
                default_amount_cents INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT
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
            CREATE TABLE transaction_splits (
                id INTEGER PRIMARY KEY,
                transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                envelope_id INTEGER NOT NULL REFERENCES envelopes(id),
                amount_cents INTEGER NOT NULL
            );
            INSERT INTO accounts (id, name, account_type, acct_key, opening_balance_cents)
            VALUES
              (1, 'Checking', 'bank', 'acct:checking', 10000),
              (2, 'Visa', 'credit_card', 'acct:visa', 0);
            INSERT INTO envelopes (id, name) VALUES (1, 'General'), (2, 'Savings');
            INSERT INTO transactions (id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid)
            VALUES
              (1, 1, 'income', 5000, '2026-05-01', 'Employer', 'paycheck', 'FIT-1'),
              (2, 1, 'expense', -1500, '2026-05-02', 'Grocer', 'food', 'FIT-2'),
              (3, 1, 'expense', -700, '2026-05-03', 'Cafe', 'coffee', 'FIT-3'),
              (4, 2, 'expense', -2200, '2026-05-04', 'Card Cafe', 'owed', 'FIT-4');
            INSERT INTO transactions (id, account_id, ttype, amount_cents, posted_at, payee, memo, xfer_pair_id)
            VALUES
              (10, 1, 'transfer_out', -2000, '2026-05-05', 'Visa', 'payment', 11),
              (11, 2, 'transfer_in', 2000, '2026-05-05', 'Checking', 'payment', 10);
            INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents)
            VALUES (1, 1, 5000);
            """
        )
        run_schema_migrations(conn)
        conn.close()

    def _closed_session(self, tx_ids=(1, 2), statement_balance=13500) -> int:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-31",
            statement_balance_cents=statement_balance,
        )
        ReconciliationService.set_cleared_transactions(session_id, list(tx_ids))
        ReconciliationService.close_session(session_id)
        return session_id

    def test_route_smoke_and_form_validation(self) -> None:
        response = self.client.get("/reconcile/accounts/1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Reconcile Checking", response.get_data(as_text=True))

        bad = self.client.post(
            "/reconcile/accounts/1/start",
            data={"statement_date": "", "statement_balance": "12.00"},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertIn("Statement date is required", bad.get_data(as_text=True))

    def test_candidate_list_shows_all_unreconciled_transactions(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-02",
            statement_balance_cents=13500,
        )

        response = self.client.get(f"/reconcile/sessions/{session_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Employer", html)
        self.assertIn("Grocer", html)
        self.assertIn("Cafe", html)

    def test_session_page_updates_reconciliation_totals_client_side(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-02",
            statement_balance_cents=13500,
        )

        response = self.client.get(f"/reconcile/sessions/{session_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("data-reconciliation-session", html)
        self.assertIn('data-starting-cents="10000"', html)
        self.assertIn('data-statement-cents="13500"', html)
        self.assertIn("data-reconciliation-selected", html)
        self.assertIn("data-reconciliation-calculated", html)
        self.assertIn("data-reconciliation-difference", html)
        self.assertIn("data-reconciliation-items", html)
        self.assertIn("data-reconciliation-check-all", html)
        self.assertIn("data-reconciliation-statement-date-input", html)
        self.assertIn("Check all through statement date", html)
        self.assertIn('data-amount-cents="5000"', html)
        self.assertIn('data-posted-at="2026-05-01"', html)
        self.assertIn('data-amount-cents="-1500"', html)
        self.assertIn('data-posted-at="2026-05-02"', html)
        self.assertIn('data-posted-at="2026-05-03"', html)
        self.assertIn("updateReconciliationSummary", html)
        self.assertIn('String(input.dataset.postedAt || "") > statementDate', html)

    def test_save_progress_and_close_balanced(self) -> None:
        start = self.client.post(
            "/reconcile/accounts/1/start",
            data={"statement_date": "2026-05-31", "statement_balance": "135.00"},
            follow_redirects=False,
        )
        self.assertEqual(start.status_code, 302)
        session_id = int(start.headers["Location"].rstrip("/").split("/")[-1])

        save = self.client.post(
            f"/reconcile/sessions/{session_id}/save",
            data={
                "statement_date": "2026-05-31",
                "statement_balance": "135.00",
                "transaction_id": ["1", "2"],
            },
            follow_redirects=True,
        )
        self.assertEqual(save.status_code, 200)
        self.assertIn("progress saved", save.get_data(as_text=True))
        self.assertEqual(len(reconciliation_repo.list_items(session_id)), 2)

        close = self.client.post(f"/reconcile/sessions/{session_id}/close", follow_redirects=True)
        self.assertEqual(close.status_code, 200)
        self.assertEqual(reconciliation_repo.get_session(session_id)["status"], "closed")

    def test_close_unbalanced_rejected_without_corruption(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-05-31",
            statement_balance_cents=99999,
        )
        ReconciliationService.set_cleared_transactions(session_id, [1, 2])
        before = reconciliation_repo.list_items(session_id)

        response = self.client.post(f"/reconcile/sessions/{session_id}/close", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("difference is zero", response.get_data(as_text=True))
        self.assertEqual(reconciliation_repo.get_session(session_id)["status"], "open")
        self.assertEqual(reconciliation_repo.list_items(session_id), before)

    def test_history_detail_reopen_and_void_behavior(self) -> None:
        session_id = self._closed_session()

        history = self.client.get("/reconcile/accounts/1/history")
        self.assertEqual(history.status_code, 200)
        html = history.get_data(as_text=True)
        self.assertIn("2026-05-31", html)
        self.assertIn("closed", html)

        detail = self.client.get(f"/reconcile/sessions/{session_id}/history")
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.get_data(as_text=True)
        self.assertIn("Employer", detail_html)
        self.assertIn("Grocer", detail_html)

        reopen = self.client.post(f"/reconcile/sessions/{session_id}/reopen", follow_redirects=True)
        self.assertEqual(reopen.status_code, 200)
        self.assertEqual(reconciliation_repo.get_session(session_id)["status"], "reopened")
        self.assertEqual(
            {item["state"] for item in reconciliation_repo.list_items(session_id)},
            {"cleared"},
        )

        void = self.client.post(f"/reconcile/sessions/{session_id}/void", follow_redirects=True)
        self.assertEqual(void.status_code, 200)
        self.assertEqual(reconciliation_repo.get_session(session_id)["status"], "void")
        self.assertEqual(len(reconciliation_repo.list_items(session_id)), 2)

    def test_transaction_filters_and_reconciled_edit_delete_guards(self) -> None:
        self._closed_session()

        reconciled = self.client.get("/tx/", query_string={"reconciliation": "reconciled"})
        self.assertEqual(reconciled.status_code, 200)
        html = reconciled.get_data(as_text=True)
        self.assertIn("Employer", html)
        self.assertNotIn("Cafe", html)

        unreconciled = self.client.get("/tx/", query_string={"reconciliation": "unreconciled"})
        self.assertEqual(unreconciled.status_code, 200)
        self.assertIn("Cafe", unreconciled.get_data(as_text=True))

        edit = self.client.post(
            "/tx/1/edit",
            data={"posted_at": "2026-05-02", "payee": "Employer", "memo": "paycheck", "amount": "50.00"},
            follow_redirects=True,
        )
        self.assertEqual(edit.status_code, 200)
        self.assertIn("Reopen the reconciliation", edit.get_data(as_text=True))
        self.assertEqual(get_db().execute("SELECT posted_at FROM transactions WHERE id=1").fetchone()["posted_at"], "2026-05-01")

        delete = self.client.post("/tx/1/delete", follow_redirects=True)
        self.assertEqual(delete.status_code, 200)
        self.assertIn("Reopen the reconciliation", delete.get_data(as_text=True))
        self.assertIsNotNone(get_db().execute("SELECT id FROM transactions WHERE id=1").fetchone())

    def test_reconciled_transaction_allows_envelope_only_edit(self) -> None:
        self._closed_session()

        edit = self.client.post(
            "/tx/1/edit",
            data={
                "posted_at": "2026-05-01",
                "payee": "Employer",
                "memo": "paycheck",
                "amount": "50.00",
                "edit_amt_2": "50.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(edit.status_code, 200)
        self.assertIn("Transaction updated", edit.get_data(as_text=True))
        rows = get_db().execute(
            """
            SELECT envelope_id, amount_cents
            FROM transaction_splits
            WHERE transaction_id=1
            ORDER BY envelope_id
            """
        ).fetchall()
        self.assertEqual([(r["envelope_id"], r["amount_cents"]) for r in rows], [(2, 5000)])
        tx = get_db().execute("SELECT payee, amount_cents FROM transactions WHERE id=1").fetchone()
        self.assertEqual(tx["payee"], "Employer")
        self.assertEqual(tx["amount_cents"], 5000)

    def test_reconciled_transaction_allows_payee_only_edit(self) -> None:
        self._closed_session()

        edit = self.client.post(
            "/tx/1/edit",
            data={
                "posted_at": "2026-05-01",
                "payee": "TACOBELL",
                "memo": "paycheck",
                "amount": "50.00",
                "edit_amt_1": "50.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(edit.status_code, 200)
        self.assertIn("Transaction updated", edit.get_data(as_text=True))
        tx = get_db().execute("SELECT payee, memo, amount_cents FROM transactions WHERE id=1").fetchone()
        self.assertEqual(tx["payee"], "TACOBELL")
        self.assertEqual(tx["memo"], "paycheck")
        self.assertEqual(tx["amount_cents"], 5000)

    def test_reconciled_transaction_allows_memo_only_edit(self) -> None:
        self._closed_session()

        edit = self.client.post(
            "/tx/1/edit",
            data={
                "posted_at": "2026-05-01",
                "payee": "Employer",
                "memo": "drive thru",
                "amount": "50.00",
                "edit_amt_1": "50.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(edit.status_code, 200)
        self.assertIn("Transaction updated", edit.get_data(as_text=True))
        tx = get_db().execute("SELECT payee, memo, amount_cents FROM transactions WHERE id=1").fetchone()
        self.assertEqual(tx["payee"], "Employer")
        self.assertEqual(tx["memo"], "drive thru")
        self.assertEqual(tx["amount_cents"], 5000)

    def test_transfer_pair_mutations_block_when_either_leg_reconciled(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-06-30",
            statement_balance_cents=8000,
        )
        ReconciliationService.set_cleared_transactions(session_id, [10])
        ReconciliationService.close_session(session_id)

        edit = self.client.post(
            "/tx/transfer/10/edit",
            data={
                "from_account_id": "1",
                "to_account_id": "2",
                "amount": "25.00",
                "posted_at": "2026-05-05",
                "memo": "changed",
                "from_remainder": "1",
                "to_remainder": "1",
            },
            follow_redirects=True,
        )
        self.assertEqual(edit.status_code, 200)
        self.assertIn("Reopen the reconciliation", edit.get_data(as_text=True))
        self.assertEqual(get_db().execute("SELECT amount_cents FROM transactions WHERE id=10").fetchone()["amount_cents"], -2000)

        delete = self.client.post("/tx/10/delete", follow_redirects=True)
        self.assertEqual(delete.status_code, 200)
        self.assertIn("Reopen the reconciliation", delete.get_data(as_text=True))
        self.assertIsNotNone(get_db().execute("SELECT id FROM transactions WHERE id=11").fetchone())

    def test_reconciled_transfer_allows_envelope_only_edit(self) -> None:
        session_id = ReconciliationService.create_session(
            account_id=1,
            statement_date="2026-06-30",
            statement_balance_cents=8000,
        )
        ReconciliationService.set_cleared_transactions(session_id, [10])
        ReconciliationService.close_session(session_id)

        edit = self.client.post(
            "/tx/transfer/10/edit",
            data={
                "from_account_id": "1",
                "to_account_id": "2",
                "amount": "20.00",
                "posted_at": "2026-05-05",
                "memo": "payment",
                "from_amount_2": "20.00",
                "to_amount_1": "20.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(edit.status_code, 200)
        self.assertIn("Transfer updated", edit.get_data(as_text=True))
        rows = get_db().execute(
            """
            SELECT transaction_id, envelope_id, amount_cents
            FROM transaction_splits
            WHERE transaction_id IN (10, 11)
            ORDER BY transaction_id, envelope_id
            """
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in rows],
            [(10, 2, -2000), (11, 1, 2000)],
        )
