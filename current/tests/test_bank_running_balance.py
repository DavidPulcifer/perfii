from tests.helpers import FinanceAppTestCase
from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, envelopes_repo, transactions_repo


class BankRunningBalanceTests(FinanceAppTestCase):
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

    def _create_bank_account(self, name: str = "FIN-088 Bank") -> int:
        return accounts_repo.insert_account({"name": name, "account_type": "bank"})

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

    def test_bank_page_renders_newest_first_running_balances(self) -> None:
        self._select_user_in_client()
        account_id = self._create_bank_account("FIN-088 Render Bank")
        self._insert_tx(account_id, amount=10000, posted_at="2026-06-01", payee="Opening deposit", ttype="income")
        self._insert_tx(account_id, amount=-2500, posted_at="2026-06-02", payee="Debit")
        self._insert_tx(account_id, amount=-1000, posted_at="2026-06-03", payee="Newest debit")

        response = self.client.get(f"/bank/{account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Recent Transactions", html)
        self.assertIn("Running Balance", html)
        self.assertIn("Newest first. Running balance is the account balance after each transaction posted.", html)
        self.assertLess(html.index("Newest debit"), html.index("Debit"))
        self.assertLess(html.index("Debit"), html.index("Opening deposit"))
        self.assertGreaterEqual(html.count("$65.00"), 2)
        self.assertIn("$75.00", html)
        self.assertIn("$100.00", html)

    def test_bank_running_balance_does_not_reset_at_pagination_boundary(self) -> None:
        self._select_user_in_client()
        account_id = self._create_bank_account("FIN-088 Paged Bank")
        for idx in range(27):
            day = idx + 1
            self._insert_tx(
                account_id,
                amount=100,
                posted_at=f"2026-06-{day:02d}",
                payee=f"Deposit {day:02d}",
                ttype="income",
            )

        response = self.client.get(f"/bank/{account_id}?page=2&per_page=25")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Showing 26-27 of 27", html)
        self.assertIn("Deposit 02", html)
        self.assertIn("Deposit 01", html)
        self.assertIn("$2.00", html)
        self.assertIn("$1.00", html)
        self.assertNotIn("Deposit 27", html)

    def test_bank_page_lists_zero_balance_envelopes_and_links_names(self) -> None:
        self._select_user_in_client()
        account_id = self._create_bank_account("FIN-087 Bank")
        other_account_id = self._create_bank_account("FIN-087 Other Bank")
        global_env_id = envelopes_repo.insert_envelope({"name": "FIN-087 Global"})
        locked_zero_id = envelopes_repo.insert_envelope({"name": "FIN-087 Locked Zero", "locked_account_id": account_id})
        locked_nonzero_id = envelopes_repo.insert_envelope({
            "name": "FIN-087 Locked Nonzero",
            "locked_account_id": account_id,
        })
        other_locked_id = envelopes_repo.insert_envelope({
            "name": "FIN-087 Other Locked",
            "locked_account_id": other_account_id,
        })
        tx_id = self._insert_tx(
            account_id,
            amount=1234,
            posted_at="2026-06-01",
            payee="Envelope funding",
            ttype="income",
        )
        get_db().execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, ?)",
            (tx_id, locked_nonzero_id, 1234),
        )
        get_db().commit()

        response = self.client.get(f"/bank/{account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        envelope_section = html[html.index("Envelope balances"):html.index("<!-- Recent transactions -->")]
        self.assertIn("FIN-087 Global", envelope_section)
        self.assertIn("FIN-087 Locked Zero", envelope_section)
        self.assertIn("FIN-087 Locked Nonzero", envelope_section)
        self.assertNotIn("FIN-087 Other Locked", envelope_section)
        self.assertIn(f'href="/envelopes/{global_env_id}/"', envelope_section)
        self.assertIn(f'href="/envelopes/{locked_zero_id}/"', envelope_section)
        self.assertIn(f'href="/envelopes/{locked_nonzero_id}/"', envelope_section)
        self.assertNotIn(f'href="/envelopes/{other_locked_id}/"', envelope_section)
        self.assertIn("$0.00", envelope_section)
        self.assertIn("$12.34", envelope_section)
