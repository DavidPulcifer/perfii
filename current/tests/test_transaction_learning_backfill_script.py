import json
import sqlite3
from unittest import TestCase

from app.db import run_schema_migrations
from scripts.backfill_transaction_learning_examples import backfill_transaction_learning_examples


class TransactionLearningBackfillScriptTests(TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'bank',
                acct_key TEXT NOT NULL UNIQUE,
                display_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE envelopes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                ttype TEXT NOT NULL CHECK (ttype IN ('income','expense','transfer_in','transfer_out','allocation')),
                amount_cents INTEGER NOT NULL,
                posted_at TEXT NOT NULL,
                payee TEXT,
                memo TEXT,
                fitid TEXT,
                ignore_match INTEGER NOT NULL DEFAULT 0,
                xfer_pair_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
                external_counterparty TEXT
            );
            CREATE TABLE transaction_splits (
                id INTEGER PRIMARY KEY,
                transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                envelope_id INTEGER NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
                amount_cents INTEGER NOT NULL
            );
            INSERT INTO accounts(id, name, account_type, acct_key, display_order)
            VALUES (1, 'Checking', 'bank', 'acct:checking', 1),
                   (2, 'Example Bank Savings 0101', 'bank', 'synthetic:savings-0101', 2);
            INSERT INTO envelopes(id, name) VALUES (5, 'Buffer');
            INSERT INTO transactions(
                id, account_id, ttype, amount_cents, posted_at, payee, memo,
                fitid, ignore_match, xfer_pair_id, external_counterparty
            ) VALUES
                (10, 1, 'transfer_out', -12500, '2026-06-20', 'Example Bank - 0101',
                 'Savings transfer', 'fit-tx', 0, 11, NULL),
                (11, 2, 'transfer_in', 12500, '2026-06-20', 'Checking',
                 'Savings transfer', NULL, 0, 10, NULL);
            INSERT INTO transaction_splits(id, transaction_id, envelope_id, amount_cents)
            VALUES (30, 10, 5, -12500);
            """
        )
        run_schema_migrations(self.conn)
        self.conn.executescript(
            """
            INSERT INTO transaction_remainder_intents(
                transaction_id, envelope_id, amount_cents, created_at, updated_at
            ) VALUES (10, 5, -2500, '2026-06-20T10:00:00', '2026-06-20T10:00:00');
            INSERT INTO import_sessions(
                id, account_id, source_bankid, source_acctid, file_hash, created_at
            ) VALUES (
                1, 1, 'EXAMPLE-BANK', 'SYNTHETIC-ACCOUNT-9999',
                'synthetic-file-hash', '2026-06-20T09:00:00'
            );
            INSERT INTO import_session_rows(
                id, session_id, row_index, posted_at, amount_cents, payee, memo,
                fitid, row_fingerprint, evidence_json, transaction_id, match_type, created_at
            ) VALUES (
                1, 1, 0, '2026-06-20', -12500, 'Online Transfer',
                'to SAV ...0101 trace 000777', 'synthetic-fit-row', 'synthetic-fingerprint-row',
                '{"parser":"ofx"}', 10, 'created', '2026-06-20T09:00:00'
            );
            INSERT INTO import_row_matches(
                id, row_id, transaction_id, match_type, evidence_json, created_at
            ) VALUES (
                1, 1, 10, 'created', '{"matched":true}', '2026-06-20T09:00:00'
            );
            INSERT INTO transaction_import_validations(
                id, account_id, transaction_id, validated_at, source, fitid,
                row_fingerprint, import_session_row_id, match_type, evidence_json,
                created_at, updated_at
            ) VALUES (
                1, 1, 10, '2026-06-20T09:00:00', 'import_commit', 'synthetic-fit-row',
                'synthetic-fingerprint-row', 1, 'created', '{"validated":true}',
                '2026-06-20T09:00:00', '2026-06-20T09:00:00'
            );
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_dry_run_previews_without_inserting(self) -> None:
        summary = backfill_transaction_learning_examples(
            self.conn,
            dry_run=True,
            preview_limit=5,
            now="2026-06-21T08:00:00+00:00",
        )

        self.assertEqual(summary.candidates, 1)
        self.assertEqual(summary.inserted, 0)
        self.assertEqual(summary.skipped_duplicates, 0)
        self.assertEqual(len(summary.preview), 1)
        self.assertEqual(summary.preview[0]["evidence_quality"], "high")
        self.assertEqual(summary.preview[0]["raw_profile"]["account_suffixes"], ["0101"])
        self.assertEqual(summary.preview[0]["raw_profile"]["direction"], "to")
        count = self.conn.execute("SELECT COUNT(1) FROM transaction_learning_examples").fetchone()[0]
        self.assertEqual(count, 0)

    def test_write_backfills_snapshots_and_is_idempotent(self) -> None:
        summary = backfill_transaction_learning_examples(
            self.conn,
            dry_run=False,
            preview_limit=5,
            now="2026-06-21T08:00:00+00:00",
        )

        self.assertEqual(summary.candidates, 1)
        self.assertEqual(summary.inserted, 1)
        row = self.conn.execute("SELECT * FROM transaction_learning_examples").fetchone()
        self.assertEqual(row["source"], "backfill")
        self.assertEqual(row["evidence_quality"], "high")
        self.assertEqual(row["raw_payee"], "Online Transfer")
        self.assertEqual(row["raw_memo"], "to SAV ...0101 trace 000777")
        self.assertEqual(row["final_payee"], "Example Bank - 0101")
        self.assertEqual(row["final_memo"], "Savings transfer")
        self.assertEqual(row["transaction_type"], "transfer_out")
        self.assertEqual(row["transfer_other_account_id"], 2)

        raw_profile = json.loads(row["raw_profile_json"])
        self.assertEqual(raw_profile["account_suffixes"], ["0101"])
        self.assertIn("trace", raw_profile["noise_tokens"])
        splits = json.loads(row["splits_json"])
        self.assertEqual(splits, [{"amount_cents": -12500, "envelope_id": 5, "id": 30}])
        remainder = json.loads(row["remainder_intent_json"])
        self.assertEqual(remainder["envelope_id"], 5)
        self.assertEqual(remainder["amount_cents"], -2500)
        decision = json.loads(row["decision_json"])
        self.assertEqual(decision["kind"], "transfer")
        self.assertEqual(decision["transfer_other_account_id"], 2)
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["import_session"]["source_bankid"], "EXAMPLE-BANK")
        self.assertEqual(evidence["transaction_import_validation"]["id"], 1)

        again = backfill_transaction_learning_examples(
            self.conn,
            dry_run=False,
            now="2026-06-21T08:01:00+00:00",
        )
        self.assertEqual(again.inserted, 0)
        self.assertEqual(again.skipped_duplicates, 1)
        count = self.conn.execute("SELECT COUNT(1) FROM transaction_learning_examples").fetchone()[0]
        self.assertEqual(count, 1)

    def test_existing_example_for_same_import_row_and_transaction_is_skipped(self) -> None:
        self.conn.execute(
            """
            INSERT INTO transaction_learning_examples(
                account_id, transaction_id, import_session_row_id,
                transaction_import_validation_id, source, evidence_quality,
                dedupe_key, raw_profile_json, final_profile_json, splits_json,
                remainder_intent_json, decision_json, evidence_json, created_at, updated_at
            ) VALUES (
                1, 10, 1, 1, 'import_commit', 'high',
                'import-commit:row:1:tx:10', '{}', '{}', '[]', '{}', '{}', '{}',
                '2026-06-20T09:00:00', '2026-06-20T09:00:00'
            )
            """
        )
        summary = backfill_transaction_learning_examples(self.conn, dry_run=False)

        self.assertEqual(summary.inserted, 0)
        self.assertEqual(summary.skipped_duplicates, 1)
        count = self.conn.execute("SELECT COUNT(1) FROM transaction_learning_examples").fetchone()[0]
        self.assertEqual(count, 1)
