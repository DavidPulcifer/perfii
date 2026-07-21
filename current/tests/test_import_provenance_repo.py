import json

from app.db import get_db
from app.repositories.import_provenance_repo import (
    delete_import_session_provenance,
    get_import_session_undo_candidate,
    latest_import_session_id_for_account,
    list_import_provenance_matches,
    record_import_session_rows,
)
from app.repositories.import_validation_repo import list_transaction_import_validations_for_evidence
from app.services.transactions_service import TransactionsService
from tests.helpers import FinanceAppTestCase


class ImportProvenanceRepoTests(FinanceAppTestCase):
    def test_record_import_session_rows_records_learning_examples_and_events(self) -> None:
        created_tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-06-20",
                "payee": "Clean Coffee",
                "amount": "12.99",
                "fitid": "learn-created-fitid",
            },
            splits=[{"envelope_id": 1, "amount": "12.99"}],
        )
        manual_tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-06-21",
                "payee": "Manual Original",
                "amount": "8.50",
            },
            splits=[{"envelope_id": 1, "amount": "8.50"}],
        )

        session_id = record_import_session_rows(
            account_id=1,
            source_bankid="BANK",
            source_acctid="ACCT",
            file_hash="hash-learning",
            rows=[
                {
                    "row_index": 0,
                    "posted_at": "2026-06-20",
                    "amount_cents": -1299,
                    "payee": "SQ *COFFEE 1234",
                    "memo": "CARD TRACE 998877",
                    "fitid": "learn-created-fitid",
                    "row_fingerprint": "learn-created-fp",
                    "transaction_id": created_tx_id,
                    "transaction_ids": [created_tx_id],
                    "match_type": "created",
                    "evidence": {"parser": "test"},
                },
                {
                    "row_index": 1,
                    "posted_at": "2026-06-21",
                    "amount_cents": -850,
                    "payee": "RAW MANUAL MATCH",
                    "memo": "matched memo",
                    "fitid": "learn-manual-fitid",
                    "row_fingerprint": "learn-manual-fp",
                    "transaction_id": manual_tx_id,
                    "transaction_ids": [manual_tx_id],
                    "match_type": "manual_match",
                    "evidence": {"match": "manual"},
                },
            ],
        )

        self.assertIsNotNone(session_id)
        db = get_db()
        examples = db.execute(
            """
            SELECT transaction_id, source, evidence_quality, raw_payee, final_payee,
                   import_session_row_id, transaction_import_validation_id, decision_json
            FROM transaction_learning_examples
            WHERE transaction_id IN (?, ?)
            ORDER BY transaction_id
            """,
            (created_tx_id, manual_tx_id),
        ).fetchall()
        self.assertEqual([row["source"] for row in examples], ["import_commit", "manual_match"])
        self.assertEqual([row["evidence_quality"] for row in examples], ["high", "high"])
        self.assertEqual(examples[0]["raw_payee"], "SQ *COFFEE 1234")
        self.assertEqual(examples[0]["final_payee"], "Clean Coffee")
        self.assertIsNotNone(examples[0]["import_session_row_id"])
        self.assertIsNotNone(examples[0]["transaction_import_validation_id"])
        self.assertEqual(json.loads(examples[1]["decision_json"])["source_action"], "manual_match")

        events = db.execute(
            """
            SELECT transaction_id, source, event_type, before_json, after_json, raw_evidence_json
            FROM transaction_learning_events
            WHERE transaction_id IN (?, ?)
            ORDER BY transaction_id
            """,
            (created_tx_id, manual_tx_id),
        ).fetchall()
        self.assertEqual([row["source"] for row in events], ["import_commit", "manual_match"])
        self.assertEqual(json.loads(events[0]["before_json"]), {})
        self.assertEqual(json.loads(events[0]["after_json"])["transaction"]["id"], created_tx_id)
        self.assertEqual(json.loads(events[1]["raw_evidence_json"])["import_row"]["row_fingerprint"], "learn-manual-fp")

    def test_record_and_list_import_provenance_matches_by_source(self) -> None:
        transaction_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-05-03",
                "payee": "Coffee Shop",
                "amount": "12.99",
            },
            splits=[{"envelope_id": 1, "amount": "12.99"}],
        )
        session_id = record_import_session_rows(
            account_id=1,
            source_bankid="BANK",
            source_acctid="ACCT",
            file_hash="hash-1",
            rows=[{
                "row_index": 4,
                "posted_at": "2026-05-03",
                "amount_cents": -1299,
                "payee": "Coffee Shop",
                "memo": "Latte",
                "fitid": "fitid-1",
                "row_fingerprint": "fp-1",
                "transaction_id": transaction_id,
                "transaction_ids": [transaction_id],
                "match_type": "manual_match",
                "evidence": {"file_hash": "hash-1"},
            }],
        )

        self.assertIsNotNone(session_id)
        matches = list_import_provenance_matches(
            1,
            ["fp-1"],
            source_bankid="BANK",
            source_acctid="ACCT",
        )
        wrong_source_matches = list_import_provenance_matches(
            1,
            ["fp-1"],
            source_bankid="OTHER",
            source_acctid="ACCT",
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["row_fingerprint"], "fp-1")
        self.assertEqual(matches[0]["match_type"], "manual_match")
        self.assertEqual(wrong_source_matches, [])

    def test_import_session_undo_helpers_load_and_delete_provenance(self) -> None:
        session_id = record_import_session_rows(
            account_id=1,
            rows=[{
                "row_index": 0,
                "posted_at": "2026-05-03",
                "amount_cents": 2500,
                "payee": "Payroll",
                "memo": "",
                "fitid": "undo-fitid-1",
                "row_fingerprint": "undo-fp-1",
                "transaction_id": 1,
                "transaction_ids": [1],
                "match_type": "created",
                "evidence": {},
            }],
        )

        candidate = get_import_session_undo_candidate(session_id)

        self.assertEqual(latest_import_session_id_for_account(1), session_id)
        self.assertEqual(candidate["account_id"], 1)
        self.assertEqual(candidate["rows"][0]["transaction_id"], 1)
        self.assertEqual(candidate["rows"][0]["match_type"], "created")
        self.assertEqual(
            len(list_transaction_import_validations_for_evidence(1, fitid="undo-fitid-1")),
            1,
        )

        self.assertEqual(delete_import_session_provenance(session_id), 1)

        self.assertIsNone(get_import_session_undo_candidate(session_id))
        self.assertEqual(
            list_transaction_import_validations_for_evidence(1, fitid="undo-fitid-1"),
            [],
        )
