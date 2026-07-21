from datetime import date

from app.db import get_db, get_meta_db
from tests.helpers import FinanceAppTestCase


class InvestmentValuationEditTests(FinanceAppTestCase):
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

    def _valuation_row(self) -> dict:
        row = get_db().execute(
            """
            SELECT v.id, v.account_id, v.asof_date, v.value_cents, v.note
            FROM investment_valuations v
            JOIN accounts a ON a.id = v.account_id
            WHERE a.account_type='investment'
            ORDER BY v.id
            LIMIT 1
            """
        ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def _create_ordering_account(self) -> tuple[int, list[int]]:
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO accounts (name, account_type, acct_key, opening_balance_cents, display_order)
            VALUES ('FIN084 Ordering Investment', 'investment', 'fin084:ordering-investment', 0, 999)
            """
        )
        account_id = int(cursor.lastrowid)
        valuation_ids = []
        for asof_date, value_cents, note in (
            ("2026-01-15", 100000, "FIN084 oldest"),
            ("2026-05-01", 200000, "FIN084 newer lower id"),
            ("2026-05-01", 300000, "FIN084 newer higher id"),
        ):
            cursor = db.execute(
                """
                INSERT INTO investment_valuations (account_id, asof_date, value_cents, note)
                VALUES (?, ?, ?, ?)
                """,
                (account_id, asof_date, value_cents, note),
            )
            valuation_ids.append(int(cursor.lastrowid))
        db.commit()
        return account_id, valuation_ids

    def test_dashboard_sorts_valuation_table_descending_without_reordering_graph(self) -> None:
        self._select_user_in_client()
        account_id, valuation_ids = self._create_ordering_account()
        oldest_id, newer_lower_id, newer_higher_id = valuation_ids

        response = self.client.get(f"/invest/{account_id}")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        display_positions = [
            body.index(f'id="valuation-edit-{newer_higher_id}"'),
            body.index(f'id="valuation-edit-{newer_lower_id}"'),
            body.index(f'id="valuation-edit-{oldest_id}"'),
        ]
        self.assertEqual(display_positions, sorted(display_positions))

        graph_positions = [
            body.index('"x": "2026-01-15"'),
            body.index('"y": 1000.0'),
            body.index('"y": 2000.0'),
            body.index('"y": 3000.0'),
        ]
        self.assertEqual(graph_positions, sorted(graph_positions))

    def test_dashboard_defaults_new_valuation_date_without_changing_edit_dates(self) -> None:
        self._select_user_in_client()
        valuation = self._valuation_row()

        response = self.client.get(f"/invest/{valuation['account_id']}")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f'<input class="form-control" type="date" name="asof_date" value="{date.today().isoformat()}" required>',
            body,
        )
        self.assertIn(
            f'<input class="form-control form-control-sm" type="date" name="asof_date" value="{valuation["asof_date"]}" required>',
            body,
        )

    def test_dashboard_edit_updates_table_and_graph_data(self) -> None:
        self._select_user_in_client()
        valuation = self._valuation_row()

        response = self.client.post(
            f"/invest/valuation/{valuation['id']}/edit",
            data={
                "account_id": str(valuation["account_id"]),
                "asof_date": "2026-05-03",
                "value": "4321.99",
                "note": "Edited valuation note",
            },
            follow_redirects=True,
        )

        updated = get_db().execute(
            "SELECT asof_date, value_cents, note FROM investment_valuations WHERE id=?",
            (valuation["id"],),
        ).fetchone()
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(dict(updated), {
            "asof_date": "2026-05-03",
            "value_cents": 432199,
            "note": "Edited valuation note",
        })
        self.assertIn("Valuation updated", body)
        self.assertIn('value="2026-05-03"', body)
        self.assertIn('value="4321.99"', body)
        self.assertIn("Edited valuation note", body)
        self.assertIn('"x": "2026-05-03"', body)
        self.assertIn('"y": 4321.99', body)

    def test_dashboard_edit_rejects_invalid_value_without_writing(self) -> None:
        self._select_user_in_client()
        valuation = self._valuation_row()

        response = self.client.post(
            f"/invest/valuation/{valuation['id']}/edit",
            data={
                "account_id": str(valuation["account_id"]),
                "asof_date": "2026-05-03",
                "value": "not-a-value",
                "note": "should not be saved",
            },
            follow_redirects=True,
        )

        unchanged = get_db().execute(
            "SELECT asof_date, value_cents, note FROM investment_valuations WHERE id=?",
            (valuation["id"],),
        ).fetchone()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(dict(unchanged), {
            "asof_date": valuation["asof_date"],
            "value_cents": valuation["value_cents"],
            "note": valuation["note"],
        })
        self.assertIn("Valuation must be a valid dollar amount", response.get_data(as_text=True))

    def test_dashboard_edit_rejects_invalid_date_without_writing(self) -> None:
        self._select_user_in_client()
        valuation = self._valuation_row()

        response = self.client.post(
            f"/invest/valuation/{valuation['id']}/edit",
            data={
                "account_id": str(valuation["account_id"]),
                "asof_date": "2026-02-31",
                "value": "4321.99",
                "note": "should not be saved",
            },
            follow_redirects=True,
        )

        unchanged = get_db().execute(
            "SELECT asof_date, value_cents, note FROM investment_valuations WHERE id=?",
            (valuation["id"],),
        ).fetchone()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(dict(unchanged), {
            "asof_date": valuation["asof_date"],
            "value_cents": valuation["value_cents"],
            "note": valuation["note"],
        })
        self.assertIn("Valuation date must be a valid date", response.get_data(as_text=True))
