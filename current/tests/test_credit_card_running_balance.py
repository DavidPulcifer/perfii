from tests.helpers import FinanceAppTestCase
from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, transactions_repo, credit_repo, envelopes_repo


class CreditCardRunningBalanceTests(FinanceAppTestCase):
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

    def _create_credit_card(self, name: str = "FIN-074 Card") -> int:
        account_id = accounts_repo.insert_account({"name": name, "account_type": "credit_card"})
        credit_repo.set_credit_limit(account_id, 500000)
        return account_id

    def _insert_tx(self, account_id: int, *, amount: int, posted_at: str, payee: str, ttype: str = "expense") -> int:
        db = get_db()
        tx_id = transactions_repo.insert_transaction(
            db=db,
            account_id=account_id,
            ttype=ttype,
            amount_cents=amount,
            posted_at=posted_at,
            payee=payee,
        )
        db.commit()
        return tx_id

    def test_running_balance_is_chronological_but_returned_newest_first(self) -> None:
        account_id = self._create_credit_card()
        first = self._insert_tx(account_id, amount=-1000, posted_at="2026-06-01", payee="First charge")
        same_day_charge = self._insert_tx(account_id, amount=-2500, posted_at="2026-06-02", payee="Same day charge")
        same_day_payment = self._insert_tx(
            account_id,
            amount=500,
            posted_at="2026-06-02",
            payee="Same day payment",
            ttype="transfer_in",
        )
        newest = self._insert_tx(account_id, amount=-100, posted_at="2026-06-03", payee="Newest charge")

        rows, total = transactions_repo.list_account_transactions_with_running_balance(
            account_id=account_id,
            limit=10,
            offset=0,
        )

        self.assertEqual(total, 4)
        self.assertEqual([row["id"] for row in rows], [newest, same_day_payment, same_day_charge, first])
        self.assertEqual(
            {row["id"]: row["running_balance_cents"] for row in rows},
            {
                first: -1000,
                same_day_charge: -3500,
                same_day_payment: -3000,
                newest: -3100,
            },
        )

    def test_running_balance_does_not_reset_at_pagination_boundary(self) -> None:
        account_id = self._create_credit_card("FIN-074 Paged Card")
        first = self._insert_tx(account_id, amount=-1000, posted_at="2026-06-01", payee="First charge")
        second = self._insert_tx(account_id, amount=-2500, posted_at="2026-06-02", payee="Second charge")
        third = self._insert_tx(account_id, amount=500, posted_at="2026-06-03", payee="Payment", ttype="transfer_in")
        fourth = self._insert_tx(account_id, amount=-100, posted_at="2026-06-04", payee="Newest charge")

        rows, total = transactions_repo.list_account_transactions_with_running_balance(
            account_id=account_id,
            limit=2,
            offset=1,
        )

        self.assertEqual(total, 4)
        self.assertEqual([row["id"] for row in rows], [third, second])
        self.assertEqual([row["running_balance_cents"] for row in rows], [-3000, -3500])
        self.assertNotIn(first, [row["id"] for row in rows])
        self.assertNotIn(fourth, [row["id"] for row in rows])

    def test_credit_card_dashboard_renders_newest_first_running_balances(self) -> None:
        self._select_user_in_client()
        account_id = self._create_credit_card("FIN-074 Render Card")
        self._insert_tx(account_id, amount=-1000, posted_at="2026-06-01", payee="Older charge")
        self._insert_tx(account_id, amount=-2500, posted_at="2026-06-02", payee="Newer charge")
        self._insert_tx(account_id, amount=500, posted_at="2026-06-03", payee="Payment", ttype="transfer_in")

        response = self.client.get(f"/credit/{account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Recent Transactions", html)
        self.assertIn("Running Balance", html)
        self.assertLess(html.index("Payment"), html.index("Newer charge"))
        self.assertLess(html.index("Newer charge"), html.index("Older charge"))
        self.assertIn("$30.00 owed", html)
        self.assertIn("$35.00 owed", html)
        self.assertIn("$10.00 owed", html)

    def test_credit_card_dashboard_displays_positive_running_balance_as_credit(self) -> None:
        self._select_user_in_client()
        account_id = self._create_credit_card("FIN-074 Credit Balance Card")
        self._insert_tx(account_id, amount=1000, posted_at="2026-06-01", payee="Refund", ttype="income")

        response = self.client.get(f"/credit/{account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("$10.00 credit", html)

    def test_credit_dashboard_still_rejects_non_credit_accounts(self) -> None:
        self._select_user_in_client()
        account_id = accounts_repo.insert_account({"name": "FIN-074 Bank", "account_type": "bank"})

        response = self.client.get(f"/credit/{account_id}")

        self.assertEqual(response.status_code, 404)

    def test_credit_card_dashboard_allocation_modal_uses_transfer_ui(self) -> None:
        self._select_user_in_client()
        other_account_id = accounts_repo.insert_account({"name": "FIN-ALLOC Other", "account_type": "bank"})
        card_account_id = self._create_credit_card("FIN-ALLOC Card")
        source_env_id = envelopes_repo.insert_envelope(
            {"name": "Source Envelope", "locked_account_id": card_account_id}
        )
        other_env_id = envelopes_repo.insert_envelope(
            {"name": "Other Source Envelope", "locked_account_id": other_account_id}
        )
        card_env_id = envelopes_repo.insert_envelope(
            {"name": "Card Envelope", "locked_account_id": card_account_id, "default_amount_cents": 1234}
        )

        response = self.client.get(f"/credit/{card_account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        modal_start = html.index('id="ccAllocateModal"')
        modal_html = html[modal_start:]
        self.assertIn("Transfer to FIN-ALLOC Card Envelopes", modal_html)
        self.assertIn('name="from_envelope_id"', modal_html)
        self.assertIn(f'value="{source_env_id}"', modal_html)
        self.assertIn("FIN-ALLOC Card - Source Envelope", modal_html)
        self.assertNotIn(f'value="{other_env_id}"', modal_html)
        self.assertNotIn("FIN-ALLOC Other - Other Source Envelope", modal_html)
        self.assertIn('data-cc-transfer-total', modal_html)
        self.assertNotIn('id="alloc_total"', modal_html)
        self.assertNotIn("Total to allocate", modal_html)
        self.assertIn(f'name="alloc_mode_{card_env_id}"', modal_html)
        self.assertIn('data-show-zero-balance="1"', modal_html)
        self.assertIn("$12.34", modal_html)
