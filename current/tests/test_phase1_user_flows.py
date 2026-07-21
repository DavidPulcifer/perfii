from uuid import uuid4

from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, aggregates_repo, envelopes_repo
from app.services.transactions_service import TransactionsService
from tests.helpers import FinanceAppTestCase


class Phase1UserFlowTests(FinanceAppTestCase):
    """Focused request/form flows for Phase 1 changes.

    These tests intentionally stay close to browser behavior: GET the page,
    POST the form fields the template emits, then inspect the resulting page or
    persisted state. They cover only areas touched so far in cleanup Phase 1.
    """

    def _select_user_in_client(self) -> None:
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

    def _first_account_of_type(self, account_type: str) -> dict:
        accounts = accounts_repo.list_accounts()
        for account in accounts:
            if account.get("account_type") == account_type:
                return account
        self.fail(f"No account found for type {account_type}")

    def _first_accounts(self, count: int = 2) -> list[dict]:
        accounts = accounts_repo.list_accounts()
        self.assertGreaterEqual(len(accounts), count)
        return accounts[:count]

    def _first_envelopes(self, count: int = 2) -> list[dict]:
        envelopes = envelopes_repo.list_envelopes()
        self.assertGreaterEqual(len(envelopes), count)
        return envelopes[:count]

    def _matching_transfer_envelopes(
        self,
        account_from: dict,
        account_to: dict,
    ) -> tuple[dict, dict]:
        source_id = envelopes_repo.insert_envelope(
            {
                "name": "Synthetic Flow Transfer Source",
                "locked_account_id": account_from["id"],
            }
        )
        destination_id = envelopes_repo.insert_envelope(
            {
                "name": "Synthetic Flow Transfer Destination",
                "locked_account_id": account_to["id"],
            }
        )
        return (
            envelopes_repo.get_envelope(source_id),
            envelopes_repo.get_envelope(destination_id),
        )

    def _insert_synthetic_loan(self) -> int:
        db = get_db()
        acct_key = f"test:phase1-flow-loan-{uuid4().hex}"
        db.execute(
            """
            INSERT INTO accounts (name, account_type, acct_key, opening_balance_cents, display_order)
            VALUES (?, 'loan', ?, 0, 999)
            """,
            ("Phase 1 Flow Loan", acct_key),
        )
        loan_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo, ignore_match)
            VALUES (?, 'expense', -100000, '2026-05-01', 'Loan setup', NULL, 1)
            """,
            (loan_id,),
        )
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo, ignore_match)
            VALUES (?, 'income', 10000, '2026-05-02', 'Loan payment', NULL, 1)
            """,
            (loan_id,),
        )
        payment_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        db.execute(
            "INSERT INTO loan_payment_parts (payment_tx_id, part_type, amount_cents, note) VALUES (?, 'principal', 8000, NULL)",
            (payment_id,),
        )
        db.execute(
            "INSERT INTO loan_payment_parts (payment_tx_id, part_type, amount_cents, note) VALUES (?, 'interest', 2000, NULL)",
            (payment_id,),
        )
        db.commit()
        return loan_id

    def test_account_edit_form_flow_has_no_note_and_persists_remaining_fields(self) -> None:
        self._select_user_in_client()
        account_id = self._first_account_of_type("bank")["id"]

        get_response = self.client.get(f"/accounts/{account_id}/edit")
        self.assertEqual(get_response.status_code, 200)
        get_html = get_response.get_data(as_text=True)
        self.assertNotIn('name="note"', get_html)
        self.assertIn('name="bankid"', get_html)
        self.assertIn('name="acctid"', get_html)
        self.assertIn('name="opening_balance"', get_html)
        self.assertIn('name="opening_date"', get_html)
        self.assertIn('name="account_type"', get_html)

        post_response = self.client.post(
            f"/accounts/{account_id}/edit",
            data={
                "name": "Phase 1 Browser Flow Account",
                "account_type": "bank",
                "bankid": "FLOWBANK",
                "acctid": "FLOWACCT",
                "opening_balance": "321.09",
                "opening_date": "2026-05-03",
                # Simulate an old/stale browser or malicious client still sending note.
                # The app should ignore it because account notes are no longer a feature.
                "note": "this should not be displayed or used",
            },
            follow_redirects=True,
        )
        self.assertEqual(post_response.status_code, 200)

        account = accounts_repo.get_account(account_id)
        self.assertEqual(account["name"], "Phase 1 Browser Flow Account")
        self.assertEqual(account["bankid"], "FLOWBANK")
        self.assertEqual(account["acctid"], "FLOWACCT")
        self.assertEqual(account["opening_balance_cents"], 32109)
        self.assertEqual(account["opening_date"], "2026-05-03")

        detail_html = post_response.get_data(as_text=True)
        self.assertNotIn("this should not be displayed or used", detail_html)
        self.assertNotIn(">Note<", detail_html)

    def test_transaction_edit_form_persists_ignore_match_checkbox(self) -> None:
        self._select_user_in_client()
        account = self._first_accounts(1)[0]
        envelope = self._first_envelopes(1)[0]
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": account["id"],
                "posted_at": "2026-05-10",
                "payee": "Browser Ignore Toggle",
                "amount": "9.00",
                "ignore_match": 1,
            },
            splits=[{"envelope_id": envelope["id"], "amount": "9.00"}],
        )

        checked_response = self.client.post(
            f"/tx/{tx_id}/edit",
            data={
                "posted_at": "2026-05-11",
                "amount": "9.00",
                "payee": "Browser Ignore Toggle",
                "memo": "checked",
                "ignore_match": "1",
                f"edit_amt_{envelope['id']}": "9.00",
            },
            follow_redirects=False,
        )
        self.assertEqual(checked_response.status_code, 302)
        self.assertEqual(
            get_db().execute("SELECT ignore_match FROM transactions WHERE id=?", (tx_id,)).fetchone()["ignore_match"],
            1,
        )

        unchecked_response = self.client.post(
            f"/tx/{tx_id}/edit",
            data={
                "posted_at": "2026-05-11",
                "amount": "9.00",
                "payee": "Browser Ignore Toggle",
                "memo": "unchecked",
                f"edit_amt_{envelope['id']}": "9.00",
            },
            follow_redirects=False,
        )
        self.assertEqual(unchecked_response.status_code, 302)
        self.assertEqual(
            get_db().execute("SELECT ignore_match FROM transactions WHERE id=?", (tx_id,)).fetchone()["ignore_match"],
            0,
        )

    def test_loan_account_page_uses_principal_only_balance_semantics(self) -> None:
        self._select_user_in_client()
        loan_id = self._insert_synthetic_loan()

        response = self.client.get(f"/accounts/{loan_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(accounts_repo.get_account_balance(loan_id), -92000)
        self.assertEqual(aggregates_repo.get_account_totals()[loan_id], -92000)
        html = response.get_data(as_text=True)
        self.assertIn("$920.00", html)
        self.assertNotIn("$900.00", html)

    def test_transfer_edit_form_flow_updates_pair_in_place(self) -> None:
        self._select_user_in_client()
        account_from, account_to = self._first_accounts(2)
        envelope_from, envelope_to = self._matching_transfer_envelopes(
            account_from,
            account_to,
        )
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": account_from["id"],
                "to_account_id": account_to["id"],
                "amount": "25.00",
                "posted_at": "2026-05-01",
                "memo": "before browser-style edit",
            },
            out_splits=[{"envelope_id": envelope_from["id"], "amount": "25.00"}],
            in_splits=[{"envelope_id": envelope_to["id"], "amount": "25.00"}],
        )

        get_response = self.client.get(f"/tx/transfer/{tx_out_id}/edit")
        self.assertEqual(get_response.status_code, 200)
        get_html = get_response.get_data(as_text=True)
        self.assertIn('name="from_account_id"', get_html)
        self.assertIn('name="to_account_id"', get_html)
        self.assertIn(f'name="from_amount_{envelope_from["id"]}"', get_html)
        self.assertIn(f'name="to_amount_{envelope_to["id"]}"', get_html)

        post_response = self.client.post(
            f"/tx/transfer/{tx_out_id}/edit",
            data={
                "posted_at": "2026-05-04",
                "amount": "40.00",
                "memo": "after browser-style edit",
                "from_account_id": str(account_to["id"]),
                "to_account_id": str(account_from["id"]),
                f"from_amount_{envelope_to['id']}": "40.00",
                f"to_amount_{envelope_from['id']}": "40.00",
            },
            follow_redirects=False,
        )
        self.assertEqual(post_response.status_code, 302)

        db = get_db()
        rows = db.execute(
            "SELECT id, account_id, ttype, amount_cents, posted_at, memo, xfer_pair_id FROM transactions WHERE id IN (?, ?) ORDER BY id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[tx_out_id]["account_id"], account_to["id"])
        self.assertEqual(by_id[tx_out_id]["ttype"], "transfer_out")
        self.assertEqual(by_id[tx_out_id]["amount_cents"], -4000)
        self.assertEqual(by_id[tx_out_id]["posted_at"], "2026-05-04")
        self.assertEqual(by_id[tx_out_id]["memo"], "after browser-style edit")
        self.assertEqual(by_id[tx_out_id]["xfer_pair_id"], tx_in_id)
        self.assertEqual(by_id[tx_in_id]["account_id"], account_from["id"])
        self.assertEqual(by_id[tx_in_id]["ttype"], "transfer_in")
        self.assertEqual(by_id[tx_in_id]["amount_cents"], 4000)
        self.assertEqual(by_id[tx_in_id]["xfer_pair_id"], tx_out_id)

        splits = db.execute(
            "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, envelope_id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in splits],
            [(tx_out_id, envelope_to["id"], -4000), (tx_in_id, envelope_from["id"], 4000)],
        )

    def test_transfer_edit_form_rejects_bad_split_without_changing_existing_pair(self) -> None:
        self._select_user_in_client()
        account_from, account_to = self._first_accounts(2)
        envelope_from, envelope_to = self._matching_transfer_envelopes(
            account_from,
            account_to,
        )
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": account_from["id"],
                "to_account_id": account_to["id"],
                "amount": "25.00",
                "posted_at": "2026-05-01",
                "memo": "before rejected browser-style edit",
            },
            out_splits=[{"envelope_id": envelope_from["id"], "amount": "25.00"}],
            in_splits=[{"envelope_id": envelope_to["id"], "amount": "25.00"}],
        )

        db = get_db()
        before_transactions = [
            dict(row)
            for row in db.execute(
                "SELECT id, account_id, ttype, amount_cents, posted_at, memo, xfer_pair_id FROM transactions WHERE id IN (?, ?) ORDER BY id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]
        before_splits = [
            dict(row)
            for row in db.execute(
                "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, envelope_id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]

        response = self.client.post(
            f"/tx/transfer/{tx_out_id}/edit",
            data={
                "posted_at": "2026-05-04",
                "amount": "40.00",
                "memo": "should be rejected",
                "from_account_id": str(account_to["id"]),
                "to_account_id": str(account_from["id"]),
                # Source side under-allocates and has no remainder, so route should reject
                # before calling the atomic service update.
                f"from_amount_{envelope_to['id']}": "39.00",
                f"to_amount_{envelope_from['id']}": "40.00",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/tx/transfer/{tx_out_id}/edit", response.headers["Location"])

        after_transactions = [
            dict(row)
            for row in db.execute(
                "SELECT id, account_id, ttype, amount_cents, posted_at, memo, xfer_pair_id FROM transactions WHERE id IN (?, ?) ORDER BY id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]
        after_splits = [
            dict(row)
            for row in db.execute(
                "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, envelope_id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]
        self.assertEqual(after_transactions, before_transactions)
        self.assertEqual(after_splits, before_splits)
