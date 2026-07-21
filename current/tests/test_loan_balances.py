from uuid import uuid4

from app.db import get_db, get_meta_db
from app.repositories import accounts_repo, aggregates_repo, loans_repo
from tests.helpers import FinanceAppTestCase


class LoanBalanceTests(FinanceAppTestCase):
    def _insert_account(self, *, name: str, account_type: str = "loan") -> int:
        db = get_db()
        db.execute(
            """
            INSERT INTO accounts (name, account_type, acct_key, opening_balance_cents, display_order)
            VALUES (?, ?, ?, 0, 999)
            """,
            (name, account_type, f"test:{name.lower().replace(' ', '-')}-{uuid4().hex}")
        )
        db.commit()
        return int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def _insert_transaction(self, *, account_id: int, amount_cents: int, ttype: str) -> int:
        db = get_db()
        db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo, ignore_match)
            VALUES (?, ?, ?, '2026-05-01', 'Loan Test', NULL, 1)
            """,
            (account_id, ttype, amount_cents),
        )
        db.commit()
        return int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def _insert_loan_part(self, *, payment_tx_id: int, part_type: str, amount_cents: int) -> None:
        db = get_db()
        db.execute(
            """
            INSERT INTO loan_payment_parts (payment_tx_id, part_type, amount_cents, note)
            VALUES (?, ?, ?, NULL)
            """,
            (payment_tx_id, part_type, amount_cents),
        )
        db.commit()

    def test_loan_balance_uses_principal_only_payment_effect(self) -> None:
        loan_id = self._insert_account(name="Synthetic Loan")
        self._insert_transaction(account_id=loan_id, amount_cents=-100000, ttype="expense")
        payment_id = self._insert_transaction(account_id=loan_id, amount_cents=10000, ttype="income")
        self._insert_loan_part(payment_tx_id=payment_id, part_type="principal", amount_cents=8000)
        self._insert_loan_part(payment_tx_id=payment_id, part_type="interest", amount_cents=1500)
        self._insert_loan_part(payment_tx_id=payment_id, part_type="fees", amount_cents=500)

        # Starting debt -1000.00 plus only principal repayment +80.00 = -920.00.
        # The full +100.00 payment should not reduce principal because 20.00 is interest/fees.
        self.assertEqual(accounts_repo.get_account_balance(loan_id), -92000)

    def test_loan_balance_matches_dashboard_aggregate(self) -> None:
        loan_id = self._insert_account(name="Synthetic Aggregate Loan")
        self._insert_transaction(account_id=loan_id, amount_cents=-250000, ttype="expense")
        payment_id = self._insert_transaction(account_id=loan_id, amount_cents=30000, ttype="income")
        self._insert_loan_part(payment_tx_id=payment_id, part_type="principal", amount_cents=22000)
        self._insert_loan_part(payment_tx_id=payment_id, part_type="interest", amount_cents=7000)
        self._insert_loan_part(payment_tx_id=payment_id, part_type="fees", amount_cents=1000)

        account_balance = accounts_repo.get_account_balance(loan_id)
        aggregate_balance = aggregates_repo.get_account_totals()[loan_id]

        self.assertEqual(account_balance, -228000)
        self.assertEqual(aggregate_balance, account_balance)

    def test_non_loan_balance_is_plain_transaction_sum(self) -> None:
        bank_id = self._insert_account(name="Synthetic Bank", account_type="bank")
        self._insert_transaction(account_id=bank_id, amount_cents=50000, ttype="income")
        self._insert_transaction(account_id=bank_id, amount_cents=-12345, ttype="expense")

        self.assertEqual(accounts_repo.get_account_balance(bank_id), 37655)
        self.assertEqual(aggregates_repo.get_account_totals()[bank_id], 37655)

    def test_loans_repo_get_loan_uses_account_id_primary_key(self) -> None:
        loan_account_id = self._insert_account(name="Synthetic Loan Row")
        db = get_db()
        db.execute(
            """
            INSERT INTO loans (account_id, original_principal_cents, normal_monthly_payment_cents, note)
            VALUES (?, ?, ?, ?)
            """,
            (loan_account_id, 123456, 7890, "current schema key"),
        )
        db.commit()

        loan = loans_repo.get_loan(loan_account_id)

        self.assertIsNotNone(loan)
        self.assertEqual(loan["account_id"], loan_account_id)
        self.assertEqual(loan["original_principal_cents"], 123456)
        self.assertEqual(loan["normal_monthly_payment_cents"], 7890)

    def test_loan_dashboard_shows_normal_monthly_payment_when_available(self) -> None:
        loan_account_id = self._insert_account(name="Synthetic Loan Payment Default")
        loans_repo.upsert_loan_details(
            loan_account_id,
            original_principal_cents=1000000,
            normal_monthly_payment_cents=43210,
        )
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

        response = self.client.get(f"/loans/{loan_account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Normal monthly payment", html)
        self.assertIn("$432.10", html)


    def test_fin032_payoff_estimator_prefills_from_balance_and_normal_payment(self) -> None:
        loan_account_id = self._insert_account(name="Synthetic Payoff Estimator")
        loans_repo.upsert_loan_details(
            loan_account_id,
            original_principal_cents=2000000,
            normal_monthly_payment_cents=50000,
        )
        self._insert_transaction(account_id=loan_account_id, amount_cents=-1200000, ttype="expense")
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

        response = self.client.get(f"/loans/{loan_account_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Loan payoff estimator", html)
        self.assertIn('name="payment"', html)
        self.assertIn('value="500.00"', html)
        self.assertIn('name="principal"', html)
        self.assertIn('value="12000.00"', html)

    def test_fin032_payoff_estimator_calculates_without_writing_transactions(self) -> None:
        loan_account_id = self._insert_account(name="Synthetic Payoff Calculation")
        self._insert_transaction(account_id=loan_account_id, amount_cents=-100000, ttype="expense")
        before_count = get_db().execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

        response = self.client.get(
            f"/loans/{loan_account_id}",
            query_string={
                "estimate": "1",
                "principal": "1000.00",
                "apr": "12",
                "payment": "100.00",
                "one_time": "100.00",
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Estimate result", html)
        self.assertIn("Payoff timeline", html)
        self.assertIn("10 months", html)
        self.assertIn("$47.93", html)
        self.assertIn("$947.93", html)
        after_count = get_db().execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
        self.assertEqual(after_count, before_count)

    def test_fin032_payoff_estimator_rejects_non_converging_payment(self) -> None:
        loan_account_id = self._insert_account(name="Synthetic Payoff No Converge")
        self._insert_transaction(account_id=loan_account_id, amount_cents=-100000, ttype="expense")
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

        response = self.client.get(
            f"/loans/{loan_account_id}",
            query_string={"estimate": "1", "principal": "1000.00", "apr": "24", "payment": "10.00", "one_time": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Payment does not cover the first month", response.get_data(as_text=True))

    def test_fin032_payoff_estimator_rejects_negative_apr(self) -> None:
        loan_account_id = self._insert_account(name="Synthetic Payoff Negative APR")
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("test user",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

        response = self.client.get(
            f"/loans/{loan_account_id}",
            query_string={"estimate": "1", "principal": "1000.00", "apr": "-1", "payment": "100.00", "one_time": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("APR must be zero or greater.", response.get_data(as_text=True))

    def test_loans_repo_does_not_expose_stale_loan_parts_or_schedule_tables(self) -> None:
        self.assertFalse(hasattr(loans_repo, "list_parts"))
        self.assertFalse(hasattr(loans_repo, "list_schedule"))
