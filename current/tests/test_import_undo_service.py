from app.db import get_db
from app.repositories import accounts_repo, transactions_repo
from app.repositories.import_provenance_repo import (
    delete_import_session_provenance,
    get_import_session_undo_candidate,
    latest_import_session_id_for_account,
    record_import_session_rows,
)
from app.repositories.import_validation_repo import list_transaction_import_validations_for_evidence
from app.services.import_undo_service import undo_import_session
from app.services.transactions_service import TransactionsService
from tests.helpers import FinanceAppTestCase


class ImportUndoServiceTests(FinanceAppTestCase):
    def _account_id(self) -> int:
        return int(accounts_repo.list_accounts()[0]["id"])

    def _create_income(self, *, fitid: str, amount_cents: int = 2500) -> int:
        return TransactionsService.create_income(
            {
                "account_id": self._account_id(),
                "amount_cents": amount_cents,
                "posted_at": "2026-06-20",
                "payee": "Undo Test",
                "memo": "",
                "fitid": fitid,
            },
            splits=[],
            allow_unallocated=True,
        )

    def _record_session(self, *, tx_id: int, fitid: str, match_type: str = "created") -> int:
        return int(record_import_session_rows(
            account_id=self._account_id(),
            rows=[{
                "row_index": 0,
                "posted_at": "2026-06-20",
                "amount_cents": 2500,
                "payee": "Undo Test",
                "memo": "",
                "fitid": fitid,
                "row_fingerprint": f"undo-fp-{fitid}",
                "transaction_id": tx_id,
                "transaction_ids": [tx_id],
                "match_type": match_type,
                "evidence": {},
            }],
        ))

    def _undo(self, session_id: int):
        return undo_import_session(
            session_id=session_id,
            get_import_session_undo_candidate_func=get_import_session_undo_candidate,
            latest_import_session_id_for_account_func=latest_import_session_id_for_account,
            delete_transaction_func=TransactionsService.delete_transaction,
            delete_import_session_provenance_func=delete_import_session_provenance,
        )

    def test_undo_import_session_removes_created_transaction_and_provenance(self) -> None:
        tx_id = self._create_income(fitid="undo-created-fitid")
        session_id = self._record_session(tx_id=tx_id, fitid="undo-created-fitid")

        result = self._undo(session_id)

        self.assertEqual(result.category, "success")
        self.assertEqual(result.undone, 1)
        self.assertIsNone(transactions_repo.get_transaction(tx_id))
        self.assertIsNone(get_import_session_undo_candidate(session_id))
        self.assertEqual(
            list_transaction_import_validations_for_evidence(self._account_id(), fitid="undo-created-fitid"),
            [],
        )

    def test_undo_import_session_refuses_manual_matches(self) -> None:
        tx_id = self._create_income(fitid="undo-manual-fitid")
        session_id = self._record_session(
            tx_id=tx_id,
            fitid="undo-manual-fitid",
            match_type="manual_match",
        )

        result = self._undo(session_id)

        self.assertEqual(result.category, "warning")
        self.assertIn("manual matches", result.message)
        self.assertIsNotNone(transactions_repo.get_transaction(tx_id))
        self.assertIsNotNone(get_import_session_undo_candidate(session_id))

    def test_undo_import_session_refuses_non_latest_session(self) -> None:
        old_tx_id = self._create_income(fitid="undo-old-fitid")
        old_session_id = self._record_session(tx_id=old_tx_id, fitid="undo-old-fitid")
        newer_tx_id = self._create_income(fitid="undo-new-fitid")
        self._record_session(tx_id=newer_tx_id, fitid="undo-new-fitid")

        result = self._undo(old_session_id)

        self.assertEqual(result.category, "warning")
        self.assertIn("most recent", result.message)
        self.assertIsNotNone(transactions_repo.get_transaction(old_tx_id))

    def test_undo_import_session_removes_created_transfer_pair(self) -> None:
        accounts = accounts_repo.list_accounts()
        if len(accounts) < 2:
            self.skipTest("transfer undo test needs two accounts")
        envelopes = get_db().execute("SELECT id FROM envelopes ORDER BY id LIMIT 2").fetchall()
        if len(envelopes) < 2:
            self.skipTest("transfer undo test needs two envelopes")

        out_id, in_id = TransactionsService.create_transfer(
            {
                "from_account_id": accounts[0]["id"],
                "to_account_id": accounts[1]["id"],
                "amount_cents": 1200,
                "posted_at": "2026-06-20",
                "memo": "Undo transfer",
                "out_fitid": "undo-transfer-out",
                "in_fitid": "undo-transfer-in",
            },
            out_splits=[{"envelope_id": envelopes[0]["id"], "amount_cents": -1200}],
            in_splits=[{"envelope_id": envelopes[1]["id"], "amount_cents": 1200}],
        )
        session_id = int(record_import_session_rows(
            account_id=int(accounts[0]["id"]),
            rows=[{
                "row_index": 0,
                "posted_at": "2026-06-20",
                "amount_cents": -1200,
                "payee": "Undo transfer",
                "memo": "",
                "fitid": "undo-transfer-out",
                "row_fingerprint": "undo-transfer-fp",
                "transaction_id": out_id,
                "transaction_ids": [out_id, in_id],
                "match_type": "created",
                "evidence": {},
            }],
        ))

        result = undo_import_session(
            session_id=session_id,
            get_import_session_undo_candidate_func=get_import_session_undo_candidate,
            latest_import_session_id_for_account_func=latest_import_session_id_for_account,
            delete_transaction_func=TransactionsService.delete_transaction,
            delete_import_session_provenance_func=delete_import_session_provenance,
        )

        self.assertEqual(result.category, "success")
        self.assertEqual(result.undone, 2)
        self.assertIsNone(transactions_repo.get_transaction(out_id))
        self.assertIsNone(transactions_repo.get_transaction(in_id))
