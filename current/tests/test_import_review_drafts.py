from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.db import get_db, get_meta_db
from app.repositories.import_review_drafts_repo import (
    cleanup_expired_import_review_drafts,
    discard_import_review_draft,
    get_import_review_draft,
    save_import_review_draft,
)
from app.services.import_draft_service import build_import_draft_identity
from app.services.imports_service import import_review_context

from .helpers import FinanceAppTestCase


class ImportReviewDraftServiceTests(FinanceAppTestCase):
    def _client_user(self) -> int:
        row = get_meta_db().execute("SELECT id FROM users WHERE LOWER(name)=LOWER('test user') ORDER BY id LIMIT 1").fetchone()
        if row is None:
            row = get_meta_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        user_id = int(row["id"])
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = user_id
        return user_id

    def _account_id(self) -> int:
        return int(get_db().execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()["id"])

    def test_identity_is_stable_and_scoped_by_account_file_and_rows(self) -> None:
        parsed = {
            "_source_type": "csv",
            "_source_filename": "statement.csv",
            "file_hash": "abc123",
            "transactions": [
                {"posted_at": "2026-06-01", "amount": "-12.34", "payee": "Coffee", "memo": "Latte", "fitid": "f1"},
                {"posted_at": "2026-06-02", "amount": "50.00", "payee": "Payroll", "memo": "", "fitid": "f2"},
            ],
        }
        first = build_import_draft_identity(parsed, 1)
        self.assertEqual(first["fingerprint"], build_import_draft_identity(parsed, 1)["fingerprint"])
        self.assertNotEqual(first["fingerprint"], build_import_draft_identity(parsed, 2)["fingerprint"])
        parsed_changed = dict(parsed, file_hash="different")
        self.assertNotEqual(first["fingerprint"], build_import_draft_identity(parsed_changed, 1)["fingerprint"])
        parsed_reordered = dict(parsed, transactions=list(reversed(parsed["transactions"])))
        self.assertNotEqual(first["fingerprint"], build_import_draft_identity(parsed_reordered, 1)["fingerprint"])

    def test_repo_saves_restores_discards_and_cleans_up_in_selected_user_db(self) -> None:
        account_id = self._account_id()
        row = save_import_review_draft(
            fingerprint="draft-fp",
            account_id=account_id,
            source_type="csv",
            source_filename="statement.csv",
            file_sha256="rawhash",
            row_count=1,
            draft_json={"rows": {"0": {"payee": "Edited"}}},
            expires_at="2099-01-01T00:00:00",
        )
        self.assertEqual(row["draft_json"]["rows"]["0"]["payee"], "Edited")
        self.assertIsNotNone(get_import_review_draft("draft-fp", account_id))
        self.assertIsNone(get_import_review_draft("draft-fp", account_id + 999))

        cleanup_expired_import_review_drafts(now="2000-01-01T00:00:00")
        self.assertIsNotNone(get_import_review_draft("draft-fp", account_id))
        cleanup_expired_import_review_drafts(now="2100-01-01T00:00:00")
        self.assertIsNone(get_import_review_draft("draft-fp", account_id))

        user_db = Path(get_meta_db().execute("SELECT db_path FROM users WHERE LOWER(name)=LOWER('test user')").fetchone()["db_path"])
        with closing(sqlite3.connect(self.app_data_dir / "meta.sqlite")) as meta_conn:
            meta_tables = meta_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='import_review_drafts'"
            ).fetchall()
        with closing(sqlite3.connect(user_db)) as user_conn:
            user_tables = user_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='import_review_drafts'"
            ).fetchall()
        self.assertEqual(meta_tables, [])
        self.assertTrue(user_tables)

    def test_api_save_and_discard_are_scoped_to_current_user_account(self) -> None:
        self._client_user()
        account_id = self._account_id()
        response = self.client.post(
            "/imports/draft/save",
            json={
                "fingerprint": "api-fp",
                "account_id": account_id,
                "source_type": "csv",
                "source_filename": "api.csv",
                "file_sha256": "rawhash",
                "row_count": 2,
                "expires_at": "2099-01-01T00:00:00",
                "draft": {"rows": {"0": {"checked": False}}},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertIsNotNone(get_import_review_draft("api-fp", account_id))

        missing_scope = self.client.post("/imports/draft/save", json={"fingerprint": "bad", "account_id": 999999, "draft": {}})
        self.assertEqual(missing_scope.status_code, 400)

        discard = self.client.post("/imports/draft/discard", json={"fingerprint": "api-fp", "account_id": account_id})
        self.assertEqual(discard.status_code, 200)
        self.assertIsNone(get_import_review_draft("api-fp", account_id))

    def test_review_context_embeds_matching_draft_metadata_and_row_fingerprints(self) -> None:
        account_id = self._account_id()
        parsed = {
            "_source_type": "csv",
            "_source_filename": "draft.csv",
            "file_hash": "hash-a",
            "transactions": [{"posted_at": "2026-06-01", "amount": "-1.00", "payee": "Cafe", "memo": ""}],
        }
        identity = build_import_draft_identity(parsed, account_id)
        save_import_review_draft(
            fingerprint=identity["fingerprint"],
            account_id=account_id,
            source_type="csv",
            source_filename="draft.csv",
            file_sha256="hash-a",
            row_count=1,
            draft_json={"rows": {"0": {"payee": "Restored"}}},
            expires_at="2099-01-01T00:00:00",
        )
        context = import_review_context(
            parsed,
            list_accounts_func=lambda: [dict(get_db().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())],
            list_fitids_func=lambda _account_id: [],
            list_envelopes_func=lambda: [],
            selected_account_id=account_id,
            get_import_review_draft_func=get_import_review_draft,
            cleanup_import_review_drafts_func=lambda: 0,
        )
        self.assertEqual(context["import_draft_identity"]["fingerprint"], identity["fingerprint"])
        self.assertEqual(context["import_review_draft"]["draft"]["rows"]["0"]["payee"], "Restored")
        self.assertTrue(context["import_row_states"][0]["draft_row_fingerprint"])

    def test_explicit_discard_clears_only_matching_draft(self) -> None:
        account_id = self._account_id()
        other_account = get_db().execute("SELECT id FROM accounts WHERE id != ? ORDER BY id LIMIT 1", (account_id,)).fetchone()
        other_account_id = int(other_account["id"]) if other_account else account_id
        save_import_review_draft(
            fingerprint="same-fp",
            account_id=account_id,
            source_type="csv",
            source_filename=None,
            file_sha256=None,
            row_count=0,
            draft_json={"account": "first"},
            expires_at="2099-01-01T00:00:00",
        )
        if other_account_id != account_id:
            save_import_review_draft(
                fingerprint="other-fp",
                account_id=other_account_id,
                source_type="csv",
                source_filename=None,
                file_sha256=None,
                row_count=0,
                draft_json={"account": "second"},
                expires_at="2099-01-01T00:00:00",
            )
        discard_import_review_draft("same-fp", account_id)
        self.assertIsNone(get_import_review_draft("same-fp", account_id))
        if other_account_id != account_id:
            self.assertIsNotNone(get_import_review_draft("other-fp", other_account_id))
