from __future__ import annotations

import sqlite3

from app.db import get_db, run_schema_migrations, table_exists, index_exists
from app.repositories.import_provenance_repo import record_import_session_rows
from app.repositories.import_validation_repo import (
    get_transaction_import_validation,
    list_import_validated_fitid_rows_for_account,
    list_import_validated_fitids_for_account,
    list_import_validated_transaction_ids_for_account,
    list_transaction_import_validations_for_evidence,
    record_transaction_import_validation,
)
from app.repositories.transactions_repo import insert_transaction
from tests.helpers import FinanceAppTestCase


class ImportValidationRepoTests(FinanceAppTestCase):
    def _insert_tx(self, account_id: int, *, amount_cents: int = -1299, fitid: str | None = None) -> int:
        db = get_db()
        tx_id = insert_transaction(
            db=db,
            account_id=account_id,
            ttype="expense" if amount_cents < 0 else "income",
            amount_cents=amount_cents,
            posted_at="2026-05-03",
            payee="Coffee Shop",
            memo="Latte",
            fitid=fitid,
        )
        db.commit()
        return int(tx_id)

    def test_validation_write_is_idempotent_and_lookup_supports_fitid_and_fingerprint(self) -> None:
        tx_id = self._insert_tx(1, fitid="fitid-1")

        first_id = record_transaction_import_validation(
            account_id=1,
            transaction_id=tx_id,
            source="import_commit",
            fitid="fitid-1",
            row_fingerprint="fp-1",
            match_type="created",
            evidence={"row_index": 0},
        )
        second_id = record_transaction_import_validation(
            account_id=1,
            transaction_id=tx_id,
            source="import_commit",
            fitid="fitid-1",
            row_fingerprint="fp-1b",
            match_type="created",
            evidence={"row_index": 1},
        )

        self.assertEqual(first_id, second_id)
        validation = get_transaction_import_validation(1, tx_id)
        self.assertEqual(validation["fitid"], "fitid-1")
        self.assertEqual(validation["row_fingerprint"], "fp-1b")
        self.assertEqual(len(list_transaction_import_validations_for_evidence(1, fitid="fitid-1")), 1)
        self.assertEqual(len(list_transaction_import_validations_for_evidence(1, row_fingerprint="fp-1b")), 1)
        self.assertIn("fitid-1", list_import_validated_fitids_for_account(1))
        self.assertIn(tx_id, list_import_validated_transaction_ids_for_account(1))

    def test_validated_fitid_rows_ignore_unvalidated_transaction_fitids(self) -> None:
        validated_tx = self._insert_tx(1, fitid="fit-valid")
        self._insert_tx(1, fitid="fit-unvalidated")
        record_transaction_import_validation(
            account_id=1,
            transaction_id=validated_tx,
            source="import_commit",
            fitid="fit-valid",
            row_fingerprint="fp-valid",
            match_type="created",
        )

        rows = list_import_validated_fitid_rows_for_account(1)

        fitids = [row["fitid"] for row in rows]
        self.assertIn("fit-valid", fitids)
        self.assertNotIn("fit-unvalidated", fitids)

    def test_record_import_session_rows_validates_only_selected_account_leg(self) -> None:
        account_one_tx = self._insert_tx(1, amount_cents=-500, fitid="out-fit")
        account_two_tx = self._insert_tx(2, amount_cents=500, fitid="in-fit")

        record_import_session_rows(
            account_id=1,
            source_bankid="BANK",
            source_acctid="ACCT1",
            file_hash="hash-1",
            rows=[{
                "row_index": 0,
                "posted_at": "2026-05-04",
                "amount_cents": -500,
                "payee": "Transfer",
                "memo": "To savings",
                "fitid": "out-fit",
                "row_fingerprint": "fp-transfer-out",
                "transaction_id": account_one_tx,
                "transaction_ids": [account_one_tx, account_two_tx],
                "match_type": "created",
                "evidence": {"file_hash": "hash-1"},
            }],
        )

        self.assertIsNotNone(get_transaction_import_validation(1, account_one_tx))
        self.assertIsNone(get_transaction_import_validation(1, account_two_tx))
        self.assertIsNone(get_transaction_import_validation(2, account_two_tx))

    def test_record_import_session_rows_validates_manual_match(self) -> None:
        tx_id = self._insert_tx(1, amount_cents=-750)

        record_import_session_rows(
            account_id=1,
            rows=[{
                "row_index": 2,
                "posted_at": "2026-05-05",
                "amount_cents": -750,
                "payee": "Matched Payee",
                "memo": "matched",
                "fitid": "manual-fit",
                "row_fingerprint": "manual-fp",
                "transaction_id": tx_id,
                "transaction_ids": [tx_id],
                "match_type": "manual_match",
                "evidence": {"match": "manual"},
            }],
        )

        validation = get_transaction_import_validation(1, tx_id)
        self.assertIsNotNone(validation)
        self.assertEqual(validation["source"], "manual_match")
        self.assertEqual(validation["match_type"], "manual_match")
        self.assertEqual(validation["fitid"], "manual-fit")

    def test_backfill_uses_only_clear_account_transaction_evidence(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(
                """
                CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, account_type TEXT, acct_key TEXT);
                CREATE TABLE envelopes (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER, amount_cents INTEGER);
                INSERT INTO accounts(id, name, account_type, acct_key) VALUES (1, 'A', 'bank', 'a'), (2, 'B', 'bank', 'b');
                INSERT INTO transactions(id, account_id, amount_cents) VALUES (10, 1, -500), (20, 2, 500);
                CREATE TABLE import_sessions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, source_bankid TEXT, source_acctid TEXT, file_hash TEXT, created_at TEXT NOT NULL);
                CREATE TABLE import_session_rows (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, row_index INTEGER NOT NULL, posted_at TEXT, amount_cents INTEGER NOT NULL, payee TEXT, memo TEXT, fitid TEXT, row_fingerprint TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', transaction_id INTEGER, match_type TEXT, created_at TEXT NOT NULL);
                CREATE TABLE import_row_matches (id INTEGER PRIMARY KEY, row_id INTEGER NOT NULL, transaction_id INTEGER NOT NULL, match_type TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
                INSERT INTO import_sessions(id, account_id, created_at) VALUES (1, 1, '2026-05-01T00:00:00');
                INSERT INTO import_session_rows(id, session_id, row_index, amount_cents, fitid, row_fingerprint, created_at) VALUES (1, 1, 0, -500, 'fit-clear', 'fp-clear', '2026-05-01T00:00:00'), (2, 1, 1, 500, 'fit-other', 'fp-other', '2026-05-01T00:00:00');
                INSERT INTO import_row_matches(row_id, transaction_id, match_type, created_at) VALUES (1, 10, 'created', '2026-05-01T00:00:00'), (2, 20, 'created', '2026-05-01T00:00:00');
                """
            )

            run_schema_migrations(conn)

            self.assertTrue(table_exists(conn, "transaction_import_validations"))
            self.assertTrue(index_exists(conn, "idx_transaction_import_validations_fitid"))
            rows = conn.execute("SELECT account_id, transaction_id, fitid, row_fingerprint, source FROM transaction_import_validations ORDER BY id").fetchall()
            self.assertEqual([dict(row) for row in rows], [{
                "account_id": 1,
                "transaction_id": 10,
                "fitid": "fit-clear",
                "row_fingerprint": "fp-clear",
                "source": "backfill",
            }])
        finally:
            conn.close()
