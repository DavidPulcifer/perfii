from app.db import get_db, get_meta_db
from tests.helpers import FinanceAppTestCase


class EnvelopeDetailTests(FinanceAppTestCase):
    def _select_user_in_client(self) -> None:
        row = get_meta_db().execute(
            "SELECT id FROM users WHERE LOWER(name)=LOWER(?) ORDER BY id LIMIT 1",
            ("default",),
        ).fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

    def _create_envelope_with_activity(self, *, archived: bool = False) -> dict:
        db = get_db()
        suffix = "archived" if archived else "active"
        account_id = db.execute(
            """
            INSERT INTO accounts (name, account_type, acct_key, opening_balance_cents)
            VALUES (?, 'bank', ?, 0)
            """,
            (f"FIN011 Checking {suffix}", f"fin011:{suffix}"),
        ).lastrowid
        envelope_id = db.execute(
            """
            INSERT INTO envelopes (name, locked_account_id, default_amount_cents, archived_at)
            VALUES (?, NULL, 0, ?)
            """,
            (f"FIN011 Groceries {suffix}", "2026-05-03T12:00:00" if archived else None),
        ).lastrowid

        income_id = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 12500, '2026-05-01', 'Employer', 'Monthly funding')
            """,
            (account_id,),
        ).lastrowid
        expense_id = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'expense', -4500, '2026-05-02', 'Market', 'Groceries')
            """,
            (account_id,),
        ).lastrowid
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, 12500)",
            (income_id, envelope_id),
        )
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, -4500)",
            (expense_id, envelope_id),
        )
        db.commit()

        return {
            "account_id": account_id,
            "envelope_id": envelope_id,
            "name": f"FIN011 Groceries {suffix}",
            "expected_total": 8000,
        }

    def test_dashboard_links_active_envelope_names_to_detail_page(self) -> None:
        created = self._create_envelope_with_activity()
        self._select_user_in_client()

        shell_response = self.client.get("/")
        self.assertEqual(shell_response.status_code, 200)
        shell_html = shell_response.get_data(as_text=True)
        self.assertIn("content: '▸';", shell_html)
        self.assertIn("content: '▾';", shell_html)

        response = self.client.get("/dashboard/balances")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        detail_url = f"/envelopes/{created['envelope_id']}/"
        master_url = f"/envelopes/by-name/{created['name'].replace(' ', '%20')}/"
        self.assertIn(f'href="{detail_url}"', html)
        self.assertIn(f'href="{master_url}"', html)
        self.assertIn('class="master-envelope-link envelope-detail-link text-reset text-decoration-none flex-grow-1"', html)
        self.assertIn('class="master-envelope-toggle collapsed"', html)
        self.assertNotIn("content: '+';", html)
        self.assertIn('aria-label="Show account balances for FIN011 Groceries active"', html)
        self.assertNotIn('onclick="event.stopPropagation()"', html)
        self.assertIn(created["name"], html)


    def test_manage_envelopes_shows_locked_account_names_not_ids(self) -> None:
        db = get_db()
        account_id = db.execute(
            """
            INSERT INTO accounts (name, account_type, acct_key, opening_balance_cents)
            VALUES ('FIN063 Named Checking', 'bank', 'fin063:named-checking', 0)
            """
        ).lastrowid
        db.execute(
            """
            INSERT INTO envelopes (name, locked_account_id, default_amount_cents)
            VALUES ('FIN063 Active Locked', ?, 0)
            """,
            (account_id,),
        )
        db.execute(
            """
            INSERT INTO envelopes (name, locked_account_id, default_amount_cents, archived_at)
            VALUES ('FIN063 Archived Locked', ?, 0, '2026-05-28T03:08:00')
            """,
            (account_id,),
        )
        db.commit()
        self._select_user_in_client()

        response = self.client.get('/envelopes/')

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('FIN063 Active Locked', html)
        self.assertIn('FIN063 Archived Locked', html)
        self.assertIn('FIN063 Named Checking', html)
        self.assertNotIn(f'#{account_id}', html)

    def test_detail_page_lists_activity_and_running_balance_matches_splits(self) -> None:
        created = self._create_envelope_with_activity()
        self._select_user_in_client()

        response = self.client.get(f"/envelopes/{created['envelope_id']}/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(created["name"], html)
        self.assertIn("Amounts in", html)
        self.assertIn("Amounts out", html)
        self.assertIn("Employer", html)
        self.assertIn("Market", html)
        self.assertLess(html.index("Market"), html.index("Employer"))
        self.assertIn("$125.00", html)
        self.assertIn("$45.00", html)
        self.assertIn("$80.00", html)
        self.assertIn('/tx/', html)

        db_total = get_db().execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transaction_splits WHERE envelope_id=?",
            (created["envelope_id"],),
        ).fetchone()["total"]
        self.assertEqual(int(db_total), created["expected_total"])

    def test_master_detail_by_name_matches_aggregated_dashboard_total(self) -> None:
        db = get_db()
        account_id = db.execute(
            """
            INSERT INTO accounts (name, account_type, acct_key, opening_balance_cents)
            VALUES ('FIN011 Shared Account', 'bank', 'fin011:shared-account', 0)
            """
        ).lastrowid
        first_envelope_id = db.execute(
            """
            INSERT INTO envelopes (name, locked_account_id, default_amount_cents)
            VALUES ('FIN011 Shared Food', NULL, 0)
            """
        ).lastrowid
        second_envelope_id = db.execute(
            """
            INSERT INTO envelopes (name, locked_account_id, default_amount_cents)
            VALUES ('FIN011 Shared Food', NULL, 0)
            """
        ).lastrowid
        first_tx_id = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'income', 3000, '2026-05-01', 'Funding', '')
            """,
            (account_id,),
        ).lastrowid
        second_tx_id = db.execute(
            """
            INSERT INTO transactions (account_id, ttype, amount_cents, posted_at, payee, memo)
            VALUES (?, 'expense', -1000, '2026-05-02', 'Market', '')
            """,
            (account_id,),
        ).lastrowid
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, 3000)",
            (first_tx_id, first_envelope_id),
        )
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (?, ?, -1000)",
            (second_tx_id, second_envelope_id),
        )
        db.commit()
        self._select_user_in_client()

        response = self.client.get("/envelopes/by-name/FIN011%20Shared%20Food/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Master detail across 2 active envelopes", html)
        self.assertIn("FIN011 Shared Food", html)
        self.assertIn("$30.00", html)
        self.assertIn("$10.00", html)
        self.assertIn("$20.00", html)

    def test_archived_envelope_detail_still_renders_history(self) -> None:
        created = self._create_envelope_with_activity(archived=True)
        self._select_user_in_client()

        response = self.client.get(f"/envelopes/{created['envelope_id']}/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Archived", html)
        self.assertIn(created["name"], html)
        self.assertIn("$80.00", html)
