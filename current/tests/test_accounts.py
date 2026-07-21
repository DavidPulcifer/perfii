from unittest.mock import patch

from app.db import get_db, get_meta_db, ensure_account_locked_unallocated_backfill
from app.repositories import accounts_repo, credit_repo, envelopes_repo, loans_repo
from app.services.transactions_service import TransactionsService
from tests.helpers import FinanceAppTestCase


class AccountEditTests(FinanceAppTestCase):

    def _active_locked_envelopes(self, account_id: int) -> list[dict]:
        rows = get_db().execute(
            """
            SELECT *
            FROM envelopes
            WHERE locked_account_id = ? AND archived_at IS NULL
            ORDER BY id
            """,
            (account_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _active_locked_unallocated_envelopes(self, account_id: int) -> list[dict]:
        return [
            envelope
            for envelope in self._active_locked_envelopes(account_id)
            if envelope["name"].lower() == "unallocated"
        ]

    def _first_account_of_type(self, account_type: str) -> dict:
        for account in accounts_repo.list_accounts():
            if account.get("account_type") == account_type:
                return account
        self.fail(f"No account found for type {account_type}")

    def test_account_edit_page_shows_bank_credit_type_toggle(self) -> None:
        self._select_user_in_client()
        account_id = self._first_account_of_type("bank")["id"]

        response = self.client.get(f"/accounts/{account_id}/edit")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('name="account_type"', html)
        self.assertIn('>Bank<', html)
        self.assertIn('>Credit Card<', html)
        self.assertIn('name="credit_limit"', html)

    def test_account_update_can_convert_bank_to_credit_card(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({"name": "Convert Me", "account_type": "bank"})

        response = self.client.post(
            f"/accounts/{account_id}/edit",
            data={
                "name": "Convert Me",
                "account_type": "credit_card",
                "bankid": "",
                "acctid": "",
                "opening_balance": "0.00",
                "opening_date": "",
                "credit_limit": "1234.56",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        account = accounts_repo.get_account(account_id)
        self.assertEqual(account["account_type"], "credit_card")
        self.assertEqual(credit_repo.get_credit_limit(account_id), 123456)

    def test_account_update_can_convert_credit_card_to_bank_and_remove_credit_limit(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({"name": "Convert Back", "account_type": "credit_card"})
        credit_repo.set_credit_limit(account_id, 555500)

        response = self.client.post(
            f"/accounts/{account_id}/edit",
            data={
                "name": "Convert Back",
                "account_type": "bank",
                "bankid": "",
                "acctid": "",
                "opening_balance": "0.00",
                "opening_date": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        account = accounts_repo.get_account(account_id)
        self.assertEqual(account["account_type"], "bank")
        self.assertIsNone(credit_repo.get_credit_limit(account_id))

    def test_account_update_conversion_preserves_transfer_links(self) -> None:
        self._select_user_in_client()
        bank_account = accounts_repo.insert_account({"name": "Convert Transfer Bank", "account_type": "bank"})
        other_account = self._first_account_of_type("bank")["id"]
        source_envelope = envelopes_repo.insert_envelope(
            {
                "name": "Synthetic Conversion Source",
                "locked_account_id": bank_account,
            }
        )
        destination_envelope = envelopes_repo.insert_envelope(
            {
                "name": "Synthetic Conversion Destination",
                "locked_account_id": other_account,
            }
        )

        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": bank_account,
                "to_account_id": other_account,
                "amount": "25.00",
                "posted_at": "2026-05-01",
                "memo": "conversion link check",
            },
            out_splits=[{"envelope_id": source_envelope, "amount": "25.00"}],
            in_splits=[{"envelope_id": destination_envelope, "amount": "25.00"}],
        )

        response = self.client.post(
            f"/accounts/{bank_account}/edit",
            data={
                "name": "Convert Transfer Bank",
                "account_type": "credit_card",
                "bankid": "",
                "acctid": "",
                "opening_balance": "0.00",
                "opening_date": "",
                "credit_limit": "5000.00",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        tx_rows = get_db().execute(
            "SELECT id, account_id, xfer_pair_id FROM transactions WHERE id IN (?, ?) ORDER BY id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        by_id = {row["id"]: row for row in tx_rows}
        self.assertEqual(by_id[tx_out_id]["account_id"], bank_account)
        self.assertEqual(by_id[tx_out_id]["xfer_pair_id"], tx_in_id)
        self.assertEqual(by_id[tx_in_id]["xfer_pair_id"], tx_out_id)

    def test_account_update_rejects_unsupported_type_conversion(self) -> None:
        self._select_user_in_client()
        loan_id = accounts_repo.insert_account({"name": "Loan Convert", "account_type": "loan"})

        response = self.client.post(
            f"/accounts/{loan_id}/edit",
            data={
                "name": "Loan Convert",
                "account_type": "bank",
                "bankid": "",
                "acctid": "",
                "opening_balance": "0.00",
                "opening_date": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        account = accounts_repo.get_account(loan_id)
        self.assertEqual(account["account_type"], "loan")

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

    def test_account_edit_page_does_not_show_account_note_field(self) -> None:
        self._select_user_in_client()
        account_id = self._first_account_of_type("bank")["id"]

        response = self.client.get(f"/accounts/{account_id}/edit")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('name="note"', html)
        self.assertNotIn('>Note<', html)

    def test_account_create_page_does_not_show_account_note_field(self) -> None:
        self._select_user_in_client()

        response = self.client.get("/accounts/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('name="note"', html)
        self.assertNotIn('>Note<', html)

    def test_account_create_page_does_not_show_credit_default_paying_bank_field(self) -> None:
        self._select_user_in_client()

        response = self.client.get("/accounts/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('name="default_paying_account_id"', html)
        self.assertNotIn('Default paying bank', html)

    def test_fin076_account_create_page_shows_loan_monthly_payment_field(self) -> None:
        self._select_user_in_client()

        response = self.client.get("/accounts/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('name="normal_monthly_payment"', html)
        self.assertIn('Normal monthly payment ($)', html)

    def test_fin076_loan_create_stores_normal_monthly_payment(self) -> None:
        self._select_user_in_client()

        response = self.client.post(
            "/accounts/new",
            data={
                "name": "FIN-076 Loan Create",
                "account_type": "loan",
                "opening_balance": "0.00",
                "opening_date": "",
                "original_principal": "12345.67",
                "normal_monthly_payment": "321.09",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        account = next(a for a in accounts_repo.list_accounts() if a["name"] == "FIN-076 Loan Create")
        loan = loans_repo.get_loan(account["id"])
        self.assertIsNotNone(loan)
        self.assertEqual(loan["original_principal_cents"], 1234567)
        self.assertEqual(loan["normal_monthly_payment_cents"], 32109)

    def test_fin076_loan_create_rejects_invalid_monthly_payment_without_writing(self) -> None:
        self._select_user_in_client()

        response = self.client.post(
            "/accounts/new",
            data={
                "name": "FIN-076 Invalid Loan",
                "account_type": "loan",
                "opening_balance": "0.00",
                "opening_date": "",
                "original_principal": "1000.00",
                "normal_monthly_payment": "not money",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(any(a["name"] == "FIN-076 Invalid Loan" for a in accounts_repo.list_accounts()))

    def test_fin076_loan_edit_shows_updates_and_clears_monthly_payment(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({"name": "FIN-076 Edit Loan", "account_type": "loan"})
        loans_repo.upsert_loan_details(
            account_id,
            original_principal_cents=500000,
            normal_monthly_payment_cents=25000,
        )

        page = self.client.get(f"/accounts/{account_id}/edit")

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('name="normal_monthly_payment"', html)
        self.assertIn('value="250.0"', html)

        update = self.client.post(
            f"/accounts/{account_id}/edit",
            data={
                "name": "FIN-076 Edit Loan Renamed",
                "account_type": "loan",
                "bankid": "",
                "acctid": "",
                "opening_balance": "0.00",
                "opening_date": "",
                "original_principal": "6000.00",
                "normal_monthly_payment": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(update.status_code, 302)
        account = accounts_repo.get_account(account_id)
        loan = loans_repo.get_loan(account_id)
        self.assertEqual(account["name"], "FIN-076 Edit Loan Renamed")
        self.assertEqual(loan["original_principal_cents"], 600000)
        self.assertIsNone(loan["normal_monthly_payment_cents"])

    def test_account_create_adds_locked_unallocated_envelope_for_transfer_capable_types(self) -> None:
        self._select_user_in_client()

        cases = [
            ("bank", {}),
            ("credit_card", {"credit_limit": "1234.00"}),
            ("loan", {}),
            ("investment", {"initial_value": "0.00", "opening_date": "2026-06-05"}),
        ]
        for account_type, extra_data in cases:
            with self.subTest(account_type=account_type):
                name = f"FIN-066 {account_type}"
                posted_data = {
                    "name": name,
                    "account_type": account_type,
                    "opening_balance": "0.00",
                    "opening_date": "",
                    **extra_data,
                }
                response = self.client.post(
                    "/accounts/new",
                    data=posted_data,
                    follow_redirects=False,
                )

                self.assertEqual(response.status_code, 302)
                account = next(a for a in accounts_repo.list_accounts() if a["name"] == name)
                envelopes = self._active_locked_unallocated_envelopes(account["id"])
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0]["default_amount_cents"], 0)

    def test_account_create_rolls_back_if_locked_unallocated_creation_fails(self) -> None:
        self._select_user_in_client()

        with patch(
            "app.blueprints.accounts.envelopes_repo.ensure_locked_unallocated_envelope",
            side_effect=RuntimeError("envelope create failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/accounts/new",
                    data={
                        "name": "FIN-066 Rollback Account",
                        "account_type": "bank",
                        "opening_balance": "0.00",
                        "opening_date": "",
                    },
                    follow_redirects=False,
                )

        self.assertFalse(
            any(a["name"] == "FIN-066 Rollback Account" for a in accounts_repo.list_accounts())
        )

    def test_locked_unallocated_helper_is_idempotent(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({"name": "FIN-066 Idempotent", "account_type": "bank"})

        first_id = envelopes_repo.ensure_locked_unallocated_envelope(account_id, account_type="bank")
        second_id = envelopes_repo.ensure_locked_unallocated_envelope(account_id, account_type="bank")

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self._active_locked_unallocated_envelopes(account_id)), 1)

    def test_backfill_adds_unallocated_only_for_accounts_without_active_locked_envelopes(self) -> None:
        self._select_user_in_client()
        missing_id = accounts_repo.insert_account({"name": "FIN-066 Missing", "account_type": "bank"})
        custom_id = accounts_repo.insert_account({"name": "FIN-066 Custom", "account_type": "bank"})
        archived_only_id = accounts_repo.insert_account({"name": "FIN-066 Archived", "account_type": "bank"})

        envelopes_repo.insert_envelope(
            {"name": "Custom Locked", "locked_account_id": custom_id, "default_amount_cents": 0}
        )
        archived_env_id = envelopes_repo.insert_envelope(
            {"name": "Archived Locked", "locked_account_id": archived_only_id, "default_amount_cents": 0}
        )
        envelopes_repo.archive_envelope(archived_env_id)

        ensure_account_locked_unallocated_backfill(get_db())
        ensure_account_locked_unallocated_backfill(get_db())

        self.assertEqual(len(self._active_locked_unallocated_envelopes(missing_id)), 1)
        self.assertEqual(len(self._active_locked_unallocated_envelopes(archived_only_id)), 1)
        self.assertEqual(self._active_locked_unallocated_envelopes(custom_id), [])
        custom_locked = self._active_locked_envelopes(custom_id)
        self.assertEqual([envelope["name"] for envelope in custom_locked], ["Custom Locked"])

    def test_credit_card_create_ignores_stale_default_paying_bank_data(self) -> None:
        self._select_user_in_client()

        response = self.client.post(
            "/accounts/new",
            data={
                "name": "No Paying Bank Card",
                "account_type": "credit_card",
                "opening_balance": "0.00",
                "opening_date": "",
                "credit_limit": "4321.00",
                "default_paying_account_id": "1",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        account = next(a for a in accounts_repo.list_accounts() if a["name"] == "No Paying Bank Card")
        self.assertEqual(account["account_type"], "credit_card")
        self.assertEqual(credit_repo.get_credit_limit(account["id"]), 432100)
        self.assertNotIn(
            "default_paying_account_id",
            [row["name"] for row in get_db().execute("PRAGMA table_info(credit_cards)").fetchall()],
        )

    def test_account_create_persists_opening_fields_on_account_row(self) -> None:
        self._select_user_in_client()

        response = self.client.post(
            "/accounts/new",
            data={
                "name": "New Opening Account",
                "account_type": "bank",
                "opening_balance": "234.56",
                "opening_date": "2026-05-02",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        accounts = accounts_repo.list_accounts()
        account = next(a for a in accounts if a["name"] == "New Opening Account")
        self.assertEqual(account["opening_balance_cents"], 23456)
        self.assertEqual(account["opening_date"], "2026-05-02")

    def test_account_create_ignores_stale_posted_note_data(self) -> None:
        self._select_user_in_client()

        response = self.client.post(
            "/accounts/new",
            data={
                "name": "No Account Note",
                "account_type": "bank",
                "opening_balance": "12.34",
                "opening_date": "2026-05-03",
                "note": "legacy account note should be ignored",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        account = next(a for a in accounts_repo.list_accounts() if a["name"] == "No Account Note")
        self.assertIsNone(account["note"])
        self.assertEqual(account["opening_balance_cents"], 1234)

        tx = get_db().execute(
            "SELECT memo FROM transactions WHERE account_id=? ORDER BY id DESC LIMIT 1",
            (account["id"],),
        ).fetchone()
        self.assertIsNotNone(tx)
        self.assertNotEqual(tx["memo"], "legacy account note should be ignored")
        self.assertNotIn("legacy account note should be ignored", response.get_data(as_text=True))

    def test_insert_account_persists_identifier_and_opening_fields(self) -> None:
        account_id = accounts_repo.insert_account(
            {
                "name": "Repo Insert Account",
                "account_type": "bank",
                "opening_balance_cents": 34567,
                "opening_date": "2026-05-03",
                "bankid": "SYNTHETIC-BANK-ID-001",
                "acctid": "SYNTHETIC-ACCOUNT-ID-001",
            }
        )

        account = accounts_repo.get_account(account_id)
        self.assertEqual(account["opening_balance_cents"], 34567)
        self.assertEqual(account["opening_date"], "2026-05-03")
        self.assertEqual(account["bankid"], "SYNTHETIC-BANK-ID-001")
        self.assertEqual(account["acctid"], "SYNTHETIC-ACCOUNT-ID-001")

    def test_account_update_persists_remaining_edit_fields(self) -> None:
        self._select_user_in_client()
        account_id = self._first_account_of_type("bank")["id"]

        response = self.client.post(
            f"/accounts/{account_id}/edit",
            data={
                "name": "Edited Test Account",
                "account_type": "bank",
                "bankid": "SYNTHETIC-BANK-ID-002",
                "acctid": "SYNTHETIC-ACCOUNT-ID-002",
                "opening_balance": "123.45",
                "opening_date": "2026-05-01",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        account = accounts_repo.get_account(account_id)
        self.assertEqual(account["name"], "Edited Test Account")
        self.assertEqual(account["account_type"], "bank")
        self.assertEqual(account["bankid"], "SYNTHETIC-BANK-ID-002")
        self.assertEqual(account["acctid"], "SYNTHETIC-ACCOUNT-ID-002")
        self.assertEqual(account["opening_balance_cents"], 12345)
        self.assertEqual(account["opening_date"], "2026-05-01")

    def test_account_update_can_clear_opening_balance_and_date(self) -> None:
        self._select_user_in_client()
        account_id = self._first_account_of_type("bank")["id"]

        response = self.client.post(
            f"/accounts/{account_id}/edit",
            data={
                "name": "Cleared Opening Account",
                "account_type": "bank",
                "bankid": "",
                "acctid": "",
                "opening_balance": "123.45",
                "opening_date": "2026-05-01",
                "clear_opening": "1",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        account = accounts_repo.get_account(account_id)
        self.assertEqual(account["opening_balance_cents"], 0)
        self.assertIsNone(account["opening_date"])
        self.assertIsNone(account["bankid"])
        self.assertIsNone(account["acctid"])
