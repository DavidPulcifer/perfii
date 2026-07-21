from unittest.mock import patch

from app.blueprints.core import _build_dashboard_balance_panel_model, _build_dashboard_unallocated
from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, credit_repo, envelopes_repo
from tests.helpers import FinanceAppTestCase


class DashboardUnallocatedTests(FinanceAppTestCase):
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

    def test_dashboard_unallocated_separates_cash_from_credit_capacity(self) -> None:
        accounts = [
            {"id": 101, "account_type": "bank"},
            {"id": 202, "account_type": "credit_card"},
            {"id": 303, "account_type": "loan"},
        ]
        account_totals = {
            101: 100000,
            202: -20000,
            303: -90000,
        }
        balances = {
            (101, 1): 65000,
            (202, 1): 30000,
            (202, 2): -5000,
            (303, 1): 10000,
        }

        with patch("app.blueprints.core.credit_repo.get_credit_limits", return_value={202: 100000}):
            result = _build_dashboard_unallocated(accounts, account_totals, balances)

        self.assertEqual(result["cash_unallocated_cents"], 35000)
        self.assertEqual(result["credit_available_to_allocate_cents"], 55000)
        self.assertEqual(result["by_account"][101]["amount_cents"], 35000)
        self.assertEqual(result["by_account"][101]["label"], "Cash unallocated")
        self.assertEqual(result["by_account"][202]["amount_cents"], 55000)
        self.assertEqual(
            result["by_account"][202]["label"],
            "Credit capacity to allocate (not cash)",
        )
        self.assertNotIn(303, result["by_account"])

    def test_positive_credit_balance_does_not_increase_dashboard_cash_unallocated(self) -> None:
        accounts = [
            {"id": 101, "account_type": "bank"},
            {"id": 202, "account_type": "credit_card"},
        ]
        account_totals = {
            101: 100000,
            202: 2500,
        }
        balances = {
            (101, 1): 65000,
        }

        with patch("app.blueprints.core.credit_repo.get_credit_limits", return_value={202: 100000}):
            result = _build_dashboard_unallocated(accounts, account_totals, balances)

        self.assertEqual(result["cash_unallocated_cents"], 35000)
        self.assertEqual(result["credit_available_to_allocate_cents"], 100000)
        self.assertEqual(result["by_account"][202]["kind"], "credit")


    def test_dashboard_balance_panel_model_precomputes_template_rows(self) -> None:
        accounts = [
            {"id": 2, "name": "Card", "account_type": "credit_card"},
            {"id": 1, "name": "Bank", "account_type": "bank"},
        ]
        envelopes = [
            {"id": 10, "name": "Groceries", "locked_account_id": None, "default_amount_cents": 0},
            {"id": 11, "name": "Groceries", "locked_account_id": 2, "default_amount_cents": 0},
            {"id": 12, "name": "Rent", "locked_account_id": 1, "default_amount_cents": 0},
        ]
        envelope_totals = {10: 1500, 11: -500, 12: 2000}
        balances = {
            (1, 10): 700,
            (2, 10): 800,
            (2, 11): -500,
            (1, 12): 2000,
        }
        dashboard_unallocated = {
            "by_account": {1: {"label": "Cash unallocated", "amount_cents": 300}}
        }

        model = _build_dashboard_balance_panel_model(
            accounts, envelopes, envelope_totals, balances, dashboard_unallocated
        )

        self.assertEqual([row["name"] for row in model["master_rows"]], ["Groceries", "Rent"])
        groceries = model["master_rows"][0]
        self.assertEqual(groceries["total"], 1000)
        self.assertEqual(
            [(row["account"]["name"], row["total"]) for row in groceries["accounts"]],
            [("Bank", 700), ("Card", 300)],
        )
        self.assertEqual([row["account"]["name"] for row in model["account_rows"]], ["Bank", "Card"])
        self.assertEqual(model["account_rows"][0]["unallocated"]["amount_cents"], 300)
        self.assertEqual(
            [(row["envelope"]["name"], row["balance"]) for row in model["account_rows"][0]["envelopes"]],
            [("Groceries", 700), ("Rent", 2000)],
        )

    def test_main_dashboard_renders_account_specific_cash_and_credit_unallocated(self) -> None:
        self._select_user_in_client()
        db = get_db()

        bank_id = accounts_repo.insert_account(
            {"name": "FIN-028 Cash Pool", "account_type": "bank"},
            db=db,
        )
        card_id = accounts_repo.insert_account(
            {"name": "FIN-028 Sample Credit Union Card", "account_type": "credit_card"},
            db=db,
        )
        credit_repo.set_credit_limit(card_id, 100000, db=db)
        cash_env_id = envelopes_repo.insert_envelope(
            {
                "name": "FIN-028 Cash Envelope",
                "locked_account_id": bank_id,
                "default_amount_cents": 0,
            }
        )
        credit_env_id = envelopes_repo.insert_envelope(
            {
                "name": "FIN-028 Card Envelope",
                "locked_account_id": card_id,
                "default_amount_cents": 0,
            }
        )
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 100000, '2026-05-04', 'FIN-028 cash', 'cash pool')
            """,
            (bank_id,),
        )
        cash_tx_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, ?)",
            (cash_tx_id, cash_env_id, 60000),
        )
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'expense', -20000, '2026-05-04', 'FIN-028 card debt', 'card owed')
            """,
            (card_id,),
        )
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'allocation', 0, '2026-05-04', 'FIN-028 card allocation', 'card envelopes')
            """,
            (card_id,),
        )
        card_alloc_tx_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, ?)",
            (card_alloc_tx_id, credit_env_id, 30000),
        )
        db.commit()

        response = self.client.get("/dashboard/balances")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("FIN-028 Cash Pool", html)
        self.assertIn("FIN-028 Sample Credit Union Card", html)
        self.assertIn("Cash unallocated", html)
        self.assertIn("$400.00", html)
        self.assertIn("Credit capacity to allocate", html)
        self.assertIn("(not cash)", html)
        self.assertIn("$500.00", html)

    def test_main_dashboard_defers_heavy_balance_panels_so_modals_are_available(self) -> None:
        self._select_user_in_client()
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-bs-target="#newExpenseModal"', html)
        self.assertIn('data-bs-target="#newIncomeModal"', html)
        self.assertIn('data-bs-target="#newTransferModal"', html)
        self.assertIn('id="newExpenseModal"', html)
        self.assertIn('id="newIncomeModal"', html)
        self.assertIn('id="newTransferModal"', html)
        self.assertIn('id="dashboard-balance-panels"', html)
        self.assertIn('/dashboard/balances', html)
        self.assertIn('Transaction buttons are ready to use while this finishes.', html)

    def test_dashboard_balance_panels_endpoint_renders_deferred_content(self) -> None:
        self._select_user_in_client()

        response = self.client.get("/dashboard/balances")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Master envelope totals", html)
        self.assertIn("Per-account balances", html)
        self.assertIn('id="dashboard-balances-json"', html)
