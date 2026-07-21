from io import BytesIO
from pathlib import Path

from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, credit_repo, envelopes_repo
from app.repositories.import_review_sources_repo import create_import_review_source
from app.repositories.import_validation_repo import record_transaction_import_validation
from app.services.transactions_service import TransactionsService
from tests.helpers import FinanceAppTestCase
from unittest.mock import patch


class Phase1AuditExistingBehaviorTests(FinanceAppTestCase):
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

    def _default_import_account(self) -> dict:
        accounts = accounts_repo.list_accounts()
        for account in accounts:
            if not (account.get("bankid") or "").strip() and not (account.get("acctid") or "").strip():
                return account
        return accounts[0]

    def _synthetic_import_source_token(self, account_id: int) -> str:
        return create_import_review_source(
            account_id=account_id,
            source_bankid=None,
            source_acctid=None,
            file_hash="synthetic-test-file",
            source_type="csv",
            source_filename="synthetic-statement.csv",
            expires_at="2099-01-01T00:00:00+00:00",
        )["token"]

    def _account_of_type(self, account_type: str) -> dict:
        for account in accounts_repo.list_accounts():
            if account.get("account_type") == account_type:
                return account
        new_id = accounts_repo.insert_account({"name": f"Test {account_type}", "account_type": account_type})
        return accounts_repo.get_account(new_id)

    def _global_envelope_pair(self) -> tuple[dict, dict]:
        """Create two synthetic envelopes that are compatible with any test account."""
        first_id = envelopes_repo.insert_envelope(
            {"name": "Synthetic Flexible A", "locked_account_id": None}
        )
        second_id = envelopes_repo.insert_envelope(
            {"name": "Synthetic Flexible B", "locked_account_id": None}
        )
        return envelopes_repo.get_envelope(first_id), envelopes_repo.get_envelope(second_id)

    def test_transfer_form_rejects_invalid_amount_without_writing(self) -> None:
        self._select_user_in_client()
        account_from, account_to = accounts_repo.list_accounts()[:2]
        envelope_from, envelope_to = self._global_envelope_pair()
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            "/tx/new/transfer",
            data={
                "from_account_id": str(account_from["id"]),
                "to_account_id": str(account_to["id"]),
                "amount": "not-a-number",
                "posted_at": "2026-05-05",
                f"transfer_from_{envelope_from['id']}": "1.00",
                f"transfer_to_{envelope_to['id']}": "1.00",
            },
            follow_redirects=False,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 302)
        self.assertEqual(after_count, before_count)

    def test_new_transfer_set_modes_create_balance_delta_splits(self) -> None:
        self._select_user_in_client()
        from_account_id = accounts_repo.insert_account({"name": "Set Mode From", "account_type": "bank"})
        to_account_id = accounts_repo.insert_account({"name": "Set Mode To", "account_type": "bank"})
        from_envelope_id = envelopes_repo.insert_envelope({"name": "Set Mode Source", "locked_account_id": from_account_id})
        to_envelope_id = envelopes_repo.insert_envelope({"name": "Set Mode Destination", "locked_account_id": to_account_id})
        db = get_db()
        from_seed_id = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 1000, '2026-07-01', 'Seed', 'source balance')
            RETURNING id
            """,
            (from_account_id,),
        ).fetchone()["id"]
        to_seed_id = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 200, '2026-07-01', 'Seed', 'destination balance')
            RETURNING id
            """,
            (to_account_id,),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, 1000)",
            (from_seed_id, from_envelope_id),
        )
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, 200)",
            (to_seed_id, to_envelope_id),
        )
        db.commit()
        before_max_id = db.execute("SELECT COALESCE(MAX(id), 0) AS id FROM transactions").fetchone()["id"]

        response = self.client.post(
            "/tx/new/transfer",
            data={
                "from_account_id": str(from_account_id),
                "to_account_id": str(to_account_id),
                "amount": "7.00",
                "posted_at": "2026-07-05",
                "memo": "regular transfer set modes",
                f"transfer_from_mode_{from_envelope_id}": "set",
                f"transfer_from_{from_envelope_id}": "3.00",
                f"transfer_to_mode_{to_envelope_id}": "set",
                f"transfer_to_{to_envelope_id}": "9.00",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        rows = db.execute(
            """
            SELECT id, account_id, ttype, amount_cents, memo, xfer_pair_id
            FROM transactions
            WHERE id > ? AND memo='regular transfer set modes'
            ORDER BY id
            """,
            (before_max_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        out_tx = next(row for row in rows if row["ttype"] == "transfer_out")
        in_tx = next(row for row in rows if row["ttype"] == "transfer_in")
        self.assertEqual(out_tx["account_id"], from_account_id)
        self.assertEqual(in_tx["account_id"], to_account_id)
        self.assertEqual(out_tx["amount_cents"], -700)
        self.assertEqual(in_tx["amount_cents"], 700)

        split_rows = db.execute(
            """
            SELECT transaction_id, envelope_id, amount_cents
            FROM transaction_splits
            WHERE transaction_id IN (?, ?)
            ORDER BY transaction_id, envelope_id
            """,
            (out_tx["id"], in_tx["id"]),
        ).fetchall()
        self.assertCountEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in split_rows],
            [(out_tx["id"], from_envelope_id, -700), (in_tx["id"], to_envelope_id, 700)],
        )

    def test_expense_form_rejects_invalid_amount_without_writing(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            "/tx/new/expense",
            data={
                "account_id": str(account["id"]),
                "posted_at": "2026-05-05",
                "payee": "Invalid amount test",
                "amount": "not-a-number",
            },
            follow_redirects=True,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_count, before_count)
        self.assertIn("Expense amount must be a valid dollar amount", response.get_data(as_text=True))

    def test_income_form_rejects_invalid_amount_without_writing(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            "/tx/new/income",
            data={
                "account_id": str(account["id"]),
                "posted_at": "2026-05-05",
                "payee": "Invalid amount test",
                "amount": "not-a-number",
            },
            follow_redirects=True,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_count, before_count)
        self.assertIn("Income amount must be a valid dollar amount", response.get_data(as_text=True))

    def test_create_expense_service_rejects_invalid_amount(self) -> None:
        with self.assertRaises(ValueError):
            TransactionsService.create_expense(
                payload={
                    "account_id": 1,
                    "posted_at": "2026-05-05",
                    "payee": "Invalid amount test",
                    "amount": "not-a-number",
                },
                splits=[],
            )

    def test_create_expense_service_rejects_zero_amount(self) -> None:
        with self.assertRaises(ValueError):
            TransactionsService.create_expense(
                payload={
                    "account_id": 1,
                    "posted_at": "2026-05-05",
                    "payee": "Zero amount test",
                    "amount": "0.00",
                },
                splits=[],
            )

    def test_create_income_service_rejects_invalid_amount(self) -> None:
        with self.assertRaises(ValueError):
            TransactionsService.create_income(
                payload={
                    "account_id": 1,
                    "posted_at": "2026-05-05",
                    "payee": "Invalid amount test",
                    "amount": "not-a-number",
                },
                splits=[],
            )

    def test_create_income_service_rejects_zero_amount(self) -> None:
        with self.assertRaises(ValueError):
            TransactionsService.create_income(
                payload={
                    "account_id": 1,
                    "posted_at": "2026-05-05",
                    "payee": "Zero amount test",
                    "amount": "0.00",
                },
                splits=[],
            )

    def test_account_creation_rejects_invalid_opening_balance_without_writing(self) -> None:
        self._select_user_in_client()
        db = get_db()
        before_accounts = db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        before_transactions = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            "/accounts/new",
            data={
                "name": "Invalid Opening Balance Account",
                "account_type": "bank",
                "opening_balance": "definitely-not-money",
                "opening_date": "2026-05-06",
            },
            follow_redirects=True,
        )

        after_accounts = db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        after_transactions = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_accounts, before_accounts)
        self.assertEqual(after_transactions, before_transactions)
        self.assertIn("Opening balance must be a valid dollar amount", response.get_data(as_text=True))

    def test_credit_card_creation_rejects_invalid_credit_limit_without_writing(self) -> None:
        self._select_user_in_client()
        db = get_db()
        before_accounts = db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        before_cards = db.execute("SELECT COUNT(*) AS c FROM credit_cards").fetchone()["c"]

        response = self.client.post(
            "/accounts/new",
            data={
                "name": "Invalid Credit Limit Card",
                "account_type": "credit_card",
                "opening_balance": "10.00",
                "opening_date": "2026-05-06",
                "credit_limit": "not-a-limit",
            },
            follow_redirects=True,
        )

        after_accounts = db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        after_cards = db.execute("SELECT COUNT(*) AS c FROM credit_cards").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_accounts, before_accounts)
        self.assertEqual(after_cards, before_cards)
        self.assertIn("Credit limit must be a valid dollar amount", response.get_data(as_text=True))

    def test_investment_creation_rejects_invalid_initial_value_without_writing(self) -> None:
        self._select_user_in_client()
        db = get_db()
        before_accounts = db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        before_valuations = db.execute("SELECT COUNT(*) AS c FROM investment_valuations").fetchone()["c"]

        response = self.client.post(
            "/accounts/new",
            data={
                "name": "Invalid Initial Value Investment",
                "account_type": "investment",
                "opening_balance": "10.00",
                "opening_date": "2026-05-06",
                "initial_value": "not-a-value",
            },
            follow_redirects=True,
        )

        after_accounts = db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        after_valuations = db.execute("SELECT COUNT(*) AS c FROM investment_valuations").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_accounts, before_accounts)
        self.assertEqual(after_valuations, before_valuations)
        self.assertIn("Initial value must be a valid dollar amount", response.get_data(as_text=True))

    def test_account_creation_rolls_back_if_related_write_fails(self) -> None:
        self._select_user_in_client()
        db = get_db()
        before_accounts = db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        before_transactions = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        with patch(
            "app.blueprints.accounts.transactions_repo.insert_transaction",
            side_effect=RuntimeError("simulated opening transaction failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/accounts/new",
                    data={
                        "name": "Should Roll Back Account",
                        "account_type": "bank",
                        "opening_balance": "10.00",
                        "opening_date": "2026-05-06",
                    },
                    follow_redirects=False,
                )

        after_accounts = db.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        after_transactions = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(after_accounts, before_accounts)
        self.assertEqual(after_transactions, before_transactions)

    def test_investment_valuation_rejects_invalid_value_without_writing(self) -> None:
        self._select_user_in_client()
        db = get_db()
        account = db.execute(
            "SELECT id FROM accounts WHERE account_type='investment' ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(account)
        before_valuations = db.execute("SELECT COUNT(*) AS c FROM investment_valuations").fetchone()["c"]

        response = self.client.post(
            "/invest/valuation/new",
            data={
                "account_id": str(account["id"]),
                "asof_date": "2026-05-07",
                "value": "not-a-value",
                "note": "invalid valuation test",
            },
            follow_redirects=True,
        )

        after_valuations = db.execute("SELECT COUNT(*) AS c FROM investment_valuations").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_valuations, before_valuations)
        self.assertIn("Valuation must be a valid dollar amount", response.get_data(as_text=True))

    def test_envelope_creation_rejects_invalid_default_amount_without_writing(self) -> None:
        self._select_user_in_client()
        db = get_db()
        before_envelopes = db.execute("SELECT COUNT(*) AS c FROM envelopes").fetchone()["c"]

        response = self.client.post(
            "/envelopes/new",
            data={"name": "Invalid Default Amount", "default_amount": "not-money"},
            follow_redirects=True,
        )

        after_envelopes = db.execute("SELECT COUNT(*) AS c FROM envelopes").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_envelopes, before_envelopes)
        self.assertIn("Default amount must be a valid dollar amount", response.get_data(as_text=True))

    def test_envelope_edit_rejects_invalid_default_amount_without_writing(self) -> None:
        self._select_user_in_client()
        env = envelopes_repo.list_envelopes()[0]
        before = envelopes_repo.get_envelope(env["id"])

        response = self.client.post(
            f"/envelopes/{env['id']}/edit",
            data={
                "name": before["name"],
                "locked_account_id": before.get("locked_account_id") or "",
                "default_amount": "not-money",
            },
            follow_redirects=True,
        )

        after = envelopes_repo.get_envelope(env["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after.get("default_amount_cents"), before.get("default_amount_cents"))
        self.assertIn("Default amount must be a valid dollar amount", response.get_data(as_text=True))

    def test_envelope_edit_persists_valid_default_amount(self) -> None:
        self._select_user_in_client()
        env = envelopes_repo.list_envelopes()[0]

        response = self.client.post(
            f"/envelopes/{env['id']}/edit",
            data={
                "name": env["name"],
                "locked_account_id": env.get("locked_account_id") or "",
                "default_amount": "12.34",
            },
            follow_redirects=False,
        )

        after = envelopes_repo.get_envelope(env["id"])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(after.get("default_amount_cents"), 1234)

    def test_envelope_create_and_edit_preserve_negative_default_amounts(self) -> None:
        self._select_user_in_client()

        create_response = self.client.post(
            "/envelopes/new",
            data={"name": "Negative Default Envelope", "default_amount": "-12.34"},
            follow_redirects=False,
        )

        created = get_db().execute(
            "SELECT * FROM envelopes WHERE name=?",
            ("Negative Default Envelope",),
        ).fetchone()
        self.assertEqual(create_response.status_code, 302)
        self.assertIsNotNone(created)
        self.assertEqual(created["default_amount_cents"], -1234)

        edit_response = self.client.post(
            f"/envelopes/{created['id']}/edit",
            data={
                "name": created["name"],
                "locked_account_id": created["locked_account_id"] or "",
                "default_amount": "-56.78",
            },
            follow_redirects=False,
        )

        after = envelopes_repo.get_envelope(created["id"])
        self.assertEqual(edit_response.status_code, 302)
        self.assertEqual(after.get("default_amount_cents"), -5678)

    def test_import_commit_rejects_invalid_amount_without_writing(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            "/imports/commit",
            data={
                "account_id": str(account["id"]),
                "import_source_token": self._synthetic_import_source_token(account["id"]),
                "count": "1",
                "row_0": "on",
                "posted_at_0": "2026-05-08",
                "amount_0": "not-money",
                "payee_0": "Invalid import amount",
                "memo_0": "should not write",
                "fitid_0": "invalid-import-amount",
            },
            follow_redirects=True,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_count, before_count)
        self.assertIn("Imported 0 transaction(s). Skipped 1.", response.get_data(as_text=True))
        self.assertIn("Row 1 amount must be a valid dollar amount", response.get_data(as_text=True))
        self.assertIn("alert-warning", response.get_data(as_text=True))

    def test_import_commit_rejects_invalid_split_without_shifting_to_remainder(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        env_a, env_b = self._global_envelope_pair()
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            "/imports/commit",
            data={
                "account_id": str(account["id"]),
                "import_source_token": self._synthetic_import_source_token(account["id"]),
                "count": "1",
                "row_0": "on",
                "posted_at_0": "2026-05-08",
                "amount_0": "-10.00",
                "payee_0": "Invalid import split",
                "memo_0": "should not write",
                "fitid_0": "invalid-import-split",
                f"exp_amount_0_{env_a['id']}": "not-money",
                "exp_remainder_0": str(env_b["id"]),
            },
            follow_redirects=True,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_count, before_count)
        self.assertIn("Imported 0 transaction(s). Skipped 1.", response.get_data(as_text=True))
        self.assertIn("Row 1 split amount must be a valid dollar amount", response.get_data(as_text=True))
        self.assertIn("alert-warning", response.get_data(as_text=True))

    def test_import_review_template_uses_server_row_state_for_duplicate_rendering(self) -> None:
        template = Path("app/templates/import_review.html").read_text()

        self.assertIn("row_state = import_row_states_by_index[i]", template)
        self.assertIn("already_imported = row_state.already_imported", template)
        self.assertNotIn("fit in existing_fitids", template)
        self.assertNotIn("i in existing_imported_row_indexes", template)

    def test_import_review_keeps_known_duplicate_visible_but_deactivated(self) -> None:
        self._select_user_in_client()
        account = self._default_import_account()
        db = get_db()
        cur = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo, fitid)
            VALUES (?, 'expense', -1234, '2026-05-08', 'Already Imported Coffee', 'Existing row', 'dupe-fit-001')
            """,
            (account["id"],),
        )
        db.commit()
        record_transaction_import_validation(
            account_id=account["id"],
            transaction_id=cur.lastrowid,
            source="import_commit",
            fitid="dupe-fit-001",
            row_fingerprint="phase1-audit-dupe-fit-001",
            match_type="created",
        )

        response = self.client.post(
            "/imports/upload",
            data={
                "account_id": str(account["id"]),
                "statement": (
                    BytesIO(
                        b"Date,Amount,Name,Memo,Id\n"
                        b"2026-05-08,-12.34,Already Imported Coffee,Existing row,dupe-fit-001\n"
                    ),
                    "statement.csv",
                )
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-fitid="dupe-fit-001"', html)
        self.assertIn('class="table-success"', html)
        self.assertNotIn('data-role="already-imported"', html)
        row_start = html.index('data-fitid="dupe-fit-001"')
        row_html = html[html.rfind('<tr', 0, row_start): html.find('</tr>', row_start)]
        self.assertIn('name="row_0"', row_html)
        self.assertIn('disabled', row_html)

    def test_credit_transfer_allocation_rejects_invalid_split_without_writing(self) -> None:
        self._select_user_in_client()
        credit_account_id = accounts_repo.insert_account({"name": "Allocate Card", "account_type": "credit_card"})
        source_env_id = envelopes_repo.insert_envelope({"name": "Source Paycheck", "locked_account_id": credit_account_id})
        dest_env_id = envelopes_repo.insert_envelope({"name": "Card Groceries", "locked_account_id": credit_account_id})
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            f"/credit/{credit_account_id}/allocate",
            data={
                "posted_at": "2026-05-09",
                "from_envelope_id": str(source_env_id),
                f"alloc_amt_{dest_env_id}": "not-money",
            },
            follow_redirects=True,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_count, before_count)
        self.assertIn("Transfer envelope amount must be a valid dollar amount", response.get_data(as_text=True))

    def test_credit_transfer_allocation_rejects_envelope_locked_to_another_account(self) -> None:
        self._select_user_in_client()
        credit_account_id = accounts_repo.insert_account(
            {"name": "Allocation Card", "account_type": "credit_card"}
        )
        other_account_id = accounts_repo.insert_account(
            {"name": "Other Synthetic Account", "account_type": "bank"}
        )
        source_env_id = envelopes_repo.insert_envelope(
            {"name": "Card Source", "locked_account_id": credit_account_id}
        )
        incompatible_env_id = envelopes_repo.insert_envelope(
            {"name": "Other Account Goal", "locked_account_id": other_account_id}
        )
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            f"/credit/{credit_account_id}/allocate",
            data={
                "posted_at": "2026-05-09",
                "from_envelope_id": str(source_env_id),
                f"alloc_amt_{incompatible_env_id}": "10.00",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Choose destination envelopes available to this credit card account",
            response.get_data(as_text=True),
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"],
            before_count,
        )

    def test_credit_transfer_allocation_creates_zero_dollar_allocation_with_set_mode_delta(self) -> None:
        self._select_user_in_client()
        credit_account_id = accounts_repo.insert_account({"name": "Transfer Card", "account_type": "credit_card"})
        source_env_id = envelopes_repo.insert_envelope({"name": "Source Envelope", "locked_account_id": credit_account_id})
        dest_env_id = envelopes_repo.insert_envelope({"name": "Card Envelope", "locked_account_id": credit_account_id})
        db = get_db()
        existing_tx_id = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 300, '2026-05-01', 'Seed', 'existing card envelope balance')
            RETURNING id
            """,
            (credit_account_id,),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, 300)",
            (existing_tx_id, dest_env_id),
        )
        db.commit()
        before_max_id = db.execute("SELECT COALESCE(MAX(id), 0) AS id FROM transactions").fetchone()["id"]

        response = self.client.post(
            f"/credit/{credit_account_id}/allocate",
            data={
                "posted_at": "2026-05-16",
                "from_envelope_id": str(source_env_id),
                "note": "card envelope transfer",
                f"alloc_mode_{dest_env_id}": "set",
                f"alloc_amt_{dest_env_id}": "10.00",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        rows = db.execute(
            """
            SELECT id, account_id, ttype, amount_cents, payee, memo, xfer_pair_id
            FROM transactions
            WHERE id > ? AND memo='card envelope transfer'
            ORDER BY id
            """,
            (before_max_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        allocation_tx = rows[0]
        self.assertEqual(allocation_tx["ttype"], "allocation")
        self.assertEqual(allocation_tx["account_id"], credit_account_id)
        self.assertEqual(allocation_tx["amount_cents"], 0)
        self.assertIsNone(allocation_tx["xfer_pair_id"])

        split_rows = db.execute(
            """
            SELECT transaction_id, envelope_id, amount_cents
            FROM transaction_splits
            WHERE transaction_id=?
            ORDER BY transaction_id, envelope_id
            """,
            (allocation_tx["id"],),
        ).fetchall()
        self.assertCountEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in split_rows],
            [
                (allocation_tx["id"], source_env_id, -700),
                (allocation_tx["id"], dest_env_id, 700),
            ],
        )
        self.assertEqual(sum(int(row["amount_cents"]) for row in split_rows), 0)
        allocation_count = db.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE id > ? AND ttype='allocation'",
            (before_max_id,),
        ).fetchone()["c"]
        self.assertEqual(allocation_count, 1)

    def test_credit_card_dashboard_shows_positive_credit_balance(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account(
            {"name": "FIN-029 Overpaid Card", "account_type": "credit_card"}
        )
        credit_repo.set_credit_limit(account_id, 100000)
        db = get_db()
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 2500, '2026-05-10', 'FIN-029 Credit', 'overpaid card')
            """,
            (account_id,),
        )
        db.commit()

        response = self.client.get(f"/credit/{account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Credit Balance", html)
        self.assertIn("$25.00 credit", html)
        self.assertIn("Card has a positive credit balance.", html)
        self.assertIn("Available to Allocate", html)
        self.assertIn("$1,000.00", html)

    def test_credit_card_dashboard_still_shows_normal_owed_balance(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account(
            {"name": "FIN-029 Owed Card", "account_type": "credit_card"}
        )
        credit_repo.set_credit_limit(account_id, 100000)
        db = get_db()
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'expense', -12345, '2026-05-10', 'FIN-029 Debt', 'owed card')
            """,
            (account_id,),
        )
        db.commit()

        response = self.client.get(f"/credit/{account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Total Owed", html)
        self.assertIn("$123.45", html)
        self.assertNotIn("Credit Balance", html)

    def test_main_dashboard_marks_positive_credit_card_balance_as_credit(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account(
            {"name": "FIN-029 Main Dashboard Credit", "account_type": "credit_card"}
        )
        credit_repo.set_credit_limit(account_id, 100000)
        db = get_db()
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 9876, '2026-05-10', 'FIN-029 Dashboard Credit', 'dashboard credit')
            """,
            (account_id,),
        )
        db.commit()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("FIN-029 Main Dashboard Credit", html)
        self.assertIn("$98.76", html)
        self.assertIn("<small>credit</small>", html)

    def test_loan_payment_rejects_invalid_source_split_without_writing(self) -> None:
        self._select_user_in_client()
        loan_account = self._account_of_type("loan")
        source_account = self._account_of_type("bank")
        env_a, env_b = self._global_envelope_pair()
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            f"/loans/{loan_account['id']}/payment",
            data={
                "posted_at": "2026-05-09",
                "amount": "10.00",
                "from_account_id": str(source_account["id"]),
                f"pay_amount_{env_a['id']}": "not-money",
                "pay_remainder": str(env_b["id"]),
            },
            follow_redirects=True,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_count, before_count)
        self.assertIn("Payment split amount must be a valid dollar amount", response.get_data(as_text=True))


    def test_loan_payment_remainder_can_subtract_from_source_envelope(self) -> None:
        self._select_user_in_client()
        loan_account = self._account_of_type("loan")
        source_account = self._account_of_type("bank")
        env_a, env_b = self._global_envelope_pair()
        db = get_db()
        before_max_id = db.execute("SELECT COALESCE(MAX(id), 0) AS id FROM transactions").fetchone()["id"]

        response = self.client.post(
            f"/loans/{loan_account['id']}/payment",
            data={
                "posted_at": "2026-05-16",
                "amount": "10.00",
                "from_account_id": str(source_account["id"]),
                "memo": "loan negative remainder",
                f"pay_amount_{env_a['id']}": "-12.00",
                "pay_remainder": str(env_b["id"]),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        rows = db.execute(
            "SELECT id, ttype, amount_cents FROM transactions WHERE id > ? AND memo=? ORDER BY id",
            (before_max_id, "loan negative remainder"),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        out_id = next(row["id"] for row in rows if row["ttype"] == "transfer_out")
        splits = db.execute(
            "SELECT envelope_id, amount_cents FROM transaction_splits WHERE transaction_id=? ORDER BY envelope_id",
            (out_id,),
        ).fetchall()
        self.assertCountEqual([(row["envelope_id"], row["amount_cents"]) for row in splits], [(env_a["id"], -1200), (env_b["id"], 200)])

    def test_external_loan_payment_records_income_without_source_splits(self) -> None:
        self._select_user_in_client()
        loan_account = self._account_of_type("loan")
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

        response = self.client.post(
            f"/loans/{loan_account['id']}/payment",
            data={
                "posted_at": "2026-05-09",
                "amount": "10.00",
                "counterparty": "External Servicer",
                "memo": "external no source",
            },
            follow_redirects=True,
        )

        after_count = db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_count, before_count + 1)
        tx = db.execute(
            """
            SELECT id, account_id, ttype, amount_cents, payee, memo, external_counterparty
            FROM transactions
            WHERE account_id=? AND payee=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (loan_account["id"], "External Servicer"),
        ).fetchone()
        self.assertIsNotNone(tx)
        self.assertEqual(tx["ttype"], "income")
        self.assertEqual(tx["amount_cents"], 1000)
        self.assertEqual(tx["memo"], "external no source")
        self.assertEqual(tx["external_counterparty"], "External Servicer")
        split_count = db.execute(
            "SELECT COUNT(*) AS c FROM transaction_splits WHERE transaction_id=?",
            (tx["id"],),
        ).fetchone()["c"]
        self.assertEqual(split_count, 0)

    def test_loan_parts_reject_invalid_amount_without_writing(self) -> None:
        self._select_user_in_client()
        loan_account = self._account_of_type("loan")
        db = get_db()
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 1000, '2026-05-09', 'Loan payment', 'parts test')
            """,
            (loan_account["id"],),
        )
        payment_tx_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.commit()
        before_parts = db.execute("SELECT COUNT(*) AS c FROM loan_payment_parts").fetchone()["c"]

        response = self.client.post(
            f"/loans/{loan_account['id']}/parts/{payment_tx_id}/save",
            data={"principal": "not-money", "interest": "1.00", "fees": "0.00"},
            follow_redirects=True,
        )

        after_parts = db.execute("SELECT COUNT(*) AS c FROM loan_payment_parts").fetchone()["c"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(after_parts, before_parts)
        self.assertIn("Principal must be a valid dollar amount", response.get_data(as_text=True))

    def test_bulk_delete_reports_partial_failure_as_warning(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        db = get_db()
        ids = []
        for idx in range(2):
            db.execute(
                """
                INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
                VALUES (?, 'income', 100, '2026-05-09', ?, 'bulk delete test')
                """,
                (account["id"], f"Bulk delete {idx}"),
            )
            ids.append(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        db.commit()

        original_delete = TransactionsService.delete_transaction

        def delete_with_one_failure(tx_id):
            if int(tx_id) == int(ids[1]):
                raise RuntimeError("simulated delete failure")
            return original_delete(tx_id)

        with patch("app.blueprints.transactions.TransactionsService.delete_transaction", side_effect=delete_with_one_failure):
            response = self.client.post(
                "/tx/bulk",
                data={"action": "delete", "tx_id": [str(ids[0]), str(ids[1])], "return_to": "/tx/"},
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Deleted 1 transaction. Failed to delete 1.", html)
        self.assertIn("alert-warning", html)
