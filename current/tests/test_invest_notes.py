from app.db import get_db, get_meta_db, table_columns
from app.repositories import invest_repo
from tests.helpers import FinanceAppTestCase


class InvestmentNoteTests(FinanceAppTestCase):
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

    def _investment_account_id(self) -> int:
        row = get_db().execute(
            "SELECT id FROM accounts WHERE account_type='investment' ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row["id"])

    def _bank_account_id(self) -> int:
        row = get_db().execute(
            "SELECT id FROM accounts WHERE account_type='bank' ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row["id"])

    def test_schema_migration_creates_investment_notes_table(self) -> None:
        db = get_db()

        self.assertIn("investment_notes", {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        })
        self.assertEqual(
            table_columns(db, "investment_notes"),
            ["id", "account_id", "note_date", "body", "created_at", "updated_at"],
        )

    def test_repository_crud_orders_and_scopes_notes(self) -> None:
        account_id = self._investment_account_id()
        other_account_id = self._bank_account_id()

        first_id = invest_repo.insert_note({
            "account_id": account_id,
            "note_date": "2026-05-04",
            "body": "First dated note",
        })
        second_id = invest_repo.insert_note({
            "account_id": account_id,
            "note_date": "2026-05-05",
            "body": "Second dated note",
        })

        notes = invest_repo.list_notes(account_id)
        self.assertEqual([note["id"] for note in notes[:2]], [second_id, first_id])

        invest_repo.update_note(first_id, {
            "account_id": account_id,
            "note_date": "2026-05-06",
            "body": "Edited dated note",
        })
        self.assertEqual(invest_repo.get_note(first_id)["body"], "Edited dated note")

        invest_repo.delete_note(first_id, account_id=other_account_id)
        self.assertIsNotNone(invest_repo.get_note(first_id, account_id=account_id))

        invest_repo.delete_note(first_id, account_id=account_id)
        self.assertIsNone(invest_repo.get_note(first_id, account_id=account_id))

    def test_dashboard_create_note_renders_list_and_graph_marker(self) -> None:
        self._select_user_in_client()
        account_id = self._investment_account_id()

        response = self.client.post(
            "/invest/note/new",
            data={
                "account_id": str(account_id),
                "note_date": "2026-05-04",
                "body": "Fed decision reaction",
            },
            follow_redirects=True,
        )

        row = get_db().execute(
            "SELECT account_id, note_date, body FROM investment_notes WHERE account_id=?",
            (account_id,),
        ).fetchone()
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(dict(row), {
            "account_id": account_id,
            "note_date": "2026-05-04",
            "body": "Fed decision reaction",
        })
        self.assertIn("Investment note added", body)
        self.assertIn("Fed decision reaction", body)
        self.assertIn("const noteMarkers", body)
        self.assertIn('"x": "2026-05-04"', body)
        self.assertIn('"body": "Fed decision reaction"', body)
        self.assertIn("Investment Notes", body)
        self.assertIn("investmentNotePanel", body)

    def test_create_note_rejects_invalid_date_and_blank_body_without_writing(self) -> None:
        self._select_user_in_client()
        account_id = self._investment_account_id()

        bad_date = self.client.post(
            "/invest/note/new",
            data={
                "account_id": str(account_id),
                "note_date": "2026-02-31",
                "body": "Invalid date note",
            },
            follow_redirects=True,
        )
        blank_body = self.client.post(
            "/invest/note/new",
            data={
                "account_id": str(account_id),
                "note_date": "2026-05-04",
                "body": "   ",
            },
            follow_redirects=True,
        )

        count = get_db().execute(
            "SELECT COUNT(1) AS c FROM investment_notes WHERE account_id=?",
            (account_id,),
        ).fetchone()["c"]

        self.assertEqual(count, 0)
        self.assertIn("Note date must be a valid date", bad_date.get_data(as_text=True))
        self.assertIn("Note text is required", blank_body.get_data(as_text=True))

    def test_edit_and_delete_validate_account_ownership_and_type(self) -> None:
        self._select_user_in_client()
        account_id = self._investment_account_id()
        bank_account_id = self._bank_account_id()
        note_id = invest_repo.insert_note({
            "account_id": account_id,
            "note_date": "2026-05-04",
            "body": "Original note",
        })

        wrong_type_response = self.client.post(
            f"/invest/note/{note_id}/edit",
            data={
                "account_id": str(bank_account_id),
                "note_date": "2026-05-05",
                "body": "Wrong account",
            },
        )
        self.assertEqual(wrong_type_response.status_code, 404)
        self.assertEqual(invest_repo.get_note(note_id, account_id=account_id)["body"], "Original note")

        edit_response = self.client.post(
            f"/invest/note/{note_id}/edit",
            data={
                "account_id": str(account_id),
                "note_date": "2026-05-05",
                "body": "Updated note",
            },
            follow_redirects=True,
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertIn("Investment note updated", edit_response.get_data(as_text=True))
        self.assertEqual(invest_repo.get_note(note_id, account_id=account_id)["body"], "Updated note")

        delete_response = self.client.post(
            f"/invest/note/{note_id}/delete",
            data={"account_id": str(account_id)},
            follow_redirects=True,
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertIn("Investment note deleted", delete_response.get_data(as_text=True))
        self.assertIsNone(invest_repo.get_note(note_id, account_id=account_id))
