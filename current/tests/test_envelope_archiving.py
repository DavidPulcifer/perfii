from __future__ import annotations

from app.db import get_db, get_meta_db, table_columns
from app.repositories import accounts_repo, envelopes_repo, splits_repo, transactions_repo
from tests.helpers import FinanceAppTestCase


class EnvelopeArchivingTests(FinanceAppTestCase):
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

    def _create_transaction_with_envelope(self, envelope_name: str) -> tuple[int, int]:
        account = accounts_repo.list_accounts()[0]
        envelope_id = envelopes_repo.insert_envelope(
            {
                "name": envelope_name,
                "locked_account_id": None,
                "default_amount_cents": 0,
            }
        )
        db = get_db()
        tx_id = transactions_repo.insert_transaction(
            db=db,
            account_id=account["id"],
            ttype="expense",
            amount_cents=-1234,
            posted_at="2026-05-03",
            payee="Archive Test Payee",
            memo="Archive Test Memo",
        )
        transactions_repo.insert_split(
            transaction_id=tx_id,
            envelope_id=envelope_id,
            amount_cents=-1234,
        )
        return tx_id, envelope_id

    def test_archive_preserves_transaction_splits_and_names(self) -> None:
        tx_id, envelope_id = self._create_transaction_with_envelope("Archive Preserve Test")

        envelopes_repo.archive_envelope(envelope_id)

        db = get_db()
        split = db.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=? AND envelope_id=?",
            (tx_id, envelope_id),
        ).fetchone()
        self.assertIsNotNone(split)
        self.assertNotIn(envelope_id, [e["id"] for e in envelopes_repo.list_envelopes()])
        self.assertIn(envelope_id, [e["id"] for e in envelopes_repo.list_envelopes(include_archived=True)])

        rendered_splits = splits_repo.get_splits_for_transaction(tx_id)
        self.assertEqual(rendered_splits[0]["envelope_name"], "Archive Preserve Test")
        self.assertIsNotNone(rendered_splits[0]["envelope_archived_at"])

    def test_archived_envelope_hidden_from_default_list_and_dashboard(self) -> None:
        _, envelope_id = self._create_transaction_with_envelope("Archive Dashboard Hidden Test")
        envelopes_repo.archive_envelope(envelope_id)
        self._select_user_in_client()

        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Archive Dashboard Hidden Test", html)

    def test_restore_makes_envelope_visible_again(self) -> None:
        _, envelope_id = self._create_transaction_with_envelope("Archive Restore Test")
        envelopes_repo.archive_envelope(envelope_id)
        self.assertNotIn(envelope_id, [e["id"] for e in envelopes_repo.list_envelopes()])

        envelopes_repo.restore_envelope(envelope_id)

        self.assertIn(envelope_id, [e["id"] for e in envelopes_repo.list_envelopes()])

    def test_transaction_edit_renders_archived_envelope_used_by_existing_split(self) -> None:
        tx_id, envelope_id = self._create_transaction_with_envelope("Archive Edit Render Test")
        envelopes_repo.archive_envelope(envelope_id)
        self._select_user_in_client()

        response = self.client.get(f"/tx/{tx_id}/edit")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Archive Edit Render Test", html)
        self.assertIn("Archived", html)
        self.assertIn(f'name="edit_amt_{envelope_id}"', html)

    def test_locked_envelope_pages_render_account_name_label(self) -> None:
        account_id = accounts_repo.insert_account({"name": "FIN-086 Checking"})
        envelope_id = envelopes_repo.insert_envelope(
            {
                "name": "FIN-086 Locked",
                "locked_account_id": account_id,
                "default_amount_cents": 0,
            }
        )
        self._select_user_in_client()

        list_response = self.client.get("/envelopes/")
        detail_response = self.client.get(f"/envelopes/{envelope_id}/")
        list_html = list_response.get_data(as_text=True)
        detail_html = detail_response.get_data(as_text=True)

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn("Locked account FIN-086 Checking", list_html)
        self.assertIn("Locked account FIN-086 Checking", detail_html)
        self.assertNotIn(f"Locked account #{account_id}", list_html)
        self.assertNotIn(f"Locked account #{account_id}", detail_html)

    def test_envelope_archive_migration_adds_archived_at_column(self) -> None:
        self.assertIn("archived_at", table_columns(get_db(), "envelopes"))
