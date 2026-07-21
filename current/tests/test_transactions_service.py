import json

from app.db import get_db, get_meta_db
from app.repositories.import_provenance_repo import record_import_session_rows
from app.services.import_commit_service import mark_ignored_transactions
from app.services.transactions_service import TransactionsService
from tests.helpers import FinanceAppTestCase
from unittest.mock import patch
from werkzeug.datastructures import MultiDict


class TransactionsServiceTests(FinanceAppTestCase):
    def _select_user_in_client(self) -> None:
        row = get_meta_db().execute(
            "SELECT id FROM users ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        with self.client.session_transaction() as client_session:
            client_session["user_id"] = int(row["id"])

    def test_edit_transaction_records_learning_event_with_import_evidence(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-06-20",
                "payee": "Original Payee",
                "memo": "original memo",
                "amount": "12.00",
                "fitid": "learn-edit-fitid",
            },
            splits=[{"envelope_id": 1, "amount": "12.00"}],
        )
        record_import_session_rows(
            account_id=1,
            source_bankid="BANK",
            source_acctid="ACCT",
            file_hash="hash-1",
            rows=[{
                "row_index": 0,
                "posted_at": "2026-06-20",
                "amount_cents": -1200,
                "payee": "RAW COFFEE 1234",
                "memo": "CARD TRACE 998877",
                "fitid": "learn-edit-fitid",
                "row_fingerprint": "learn-edit-fp",
                "transaction_id": tx_id,
                "transaction_ids": [tx_id],
                "match_type": "created",
                "evidence": {"parser": "test"},
            }],
        )

        TransactionsService.edit_transaction(
            tx_id,
            payload={"payee": "Clean Payee", "memo": "clean memo"},
        )

        event = get_db().execute(
            """
            SELECT source, evidence_quality, before_json, after_json, raw_evidence_json
            FROM transaction_learning_events
            WHERE transaction_id=? AND source='transaction_edit'
            ORDER BY id DESC LIMIT 1
            """,
            (tx_id,),
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["evidence_quality"], "high")
        before = json.loads(event["before_json"])
        after = json.loads(event["after_json"])
        raw = json.loads(event["raw_evidence_json"])
        self.assertEqual(before["transaction"]["payee"], "Original Payee")
        self.assertEqual(after["transaction"]["payee"], "Clean Payee")
        self.assertEqual(raw["raw_payee"], "RAW COFFEE 1234")
        self.assertEqual(raw["import_row"]["row_fingerprint"], "learn-edit-fp")

        example = get_db().execute(
            """
            SELECT source, final_payee, raw_payee, transaction_import_validation_id
            FROM transaction_learning_examples
            WHERE transaction_id=? AND source='transaction_edit'
            ORDER BY id DESC LIMIT 1
            """,
            (tx_id,),
        ).fetchone()
        self.assertEqual(example["final_payee"], "Clean Payee")
        self.assertEqual(example["raw_payee"], "RAW COFFEE 1234")
        self.assertIsNotNone(example["transaction_import_validation_id"])

    def test_split_and_remainder_edits_record_learning_event_types(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-06-20",
                "payee": "Split Learn",
                "amount": "12.00",
            },
            splits=[{"envelope_id": 1, "amount": "12.00"}],
        )

        TransactionsService.edit_transaction(
            tx_id,
            payload={},
            splits=[{"envelope_id": 2, "amount": "12.00"}],
        )
        TransactionsService.edit_transaction(
            tx_id,
            payload={},
            splits=None,
            remainder_envelope_id=2,
            remainder_amount_cents=-300,
        )

        events = get_db().execute(
            """
            SELECT source, event_type, before_json, after_json
            FROM transaction_learning_events
            WHERE transaction_id=?
            ORDER BY id DESC LIMIT 2
            """,
            (tx_id,),
        ).fetchall()
        self.assertEqual(
            [(row["source"], row["event_type"]) for row in reversed(events)],
            [("split_edit", "split_edit"), ("remainder_intent_change", "remainder_intent_change")],
        )
        remainder_after = json.loads(events[0]["after_json"])["remainder_intent"]
        self.assertEqual(remainder_after["envelope_id"], 2)
        self.assertEqual(remainder_after["amount_cents"], -300)

    def test_transfer_edit_and_conversion_record_learning_events(self) -> None:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "25.00",
                "posted_at": "2026-06-20",
                "memo": "original transfer",
            },
            out_splits=[{"envelope_id": 1, "amount": "25.00"}],
            in_splits=[{"envelope_id": 6, "amount": "25.00"}],
        )

        TransactionsService.edit_transfer(
            tx_out_id,
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "30.00",
                "posted_at": "2026-06-21",
                "memo": "edited transfer",
            },
            out_splits=[{"envelope_id": 1, "amount": "30.00"}],
            in_splits=[{"envelope_id": 6, "amount": "30.00"}],
        )

        edit_events = get_db().execute(
            """
            SELECT transaction_id, source, event_type, before_json, after_json
            FROM transaction_learning_events
            WHERE transaction_id IN (?, ?) AND source='transfer_edit'
            ORDER BY transaction_id
            """,
            (tx_out_id, tx_in_id),
        ).fetchall()
        self.assertEqual(len(edit_events), 2)
        self.assertEqual({row["event_type"] for row in edit_events}, {"transfer_edit"})
        self.assertEqual(json.loads(edit_events[0]["before_json"])["transaction"]["amount_cents"], -2500)
        self.assertEqual(json.loads(edit_events[0]["after_json"])["transaction"]["amount_cents"], -3000)

        standard_tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-06-22",
                "payee": "Convert Me",
                "amount": "10.00",
            },
            splits=[{"envelope_id": 1, "amount": "10.00"}],
        )
        converted_out_id, converted_in_id = TransactionsService.convert_standard_transaction_to_transfer(
            standard_tx_id,
            other_account_id=2,
            current_splits=[{"envelope_id": 1, "amount_cents": -1000}],
            other_splits=[{"envelope_id": 6, "amount_cents": 1000}],
        )

        conversion_events = get_db().execute(
            """
            SELECT transaction_id, source, event_type, before_json, after_json
            FROM transaction_learning_events
            WHERE transaction_id IN (?, ?) AND source='transfer_conversion'
            ORDER BY transaction_id
            """,
            (converted_out_id, converted_in_id),
        ).fetchall()
        self.assertEqual(len(conversion_events), 2)
        converted_current = next(row for row in conversion_events if row["transaction_id"] == standard_tx_id)
        self.assertEqual(json.loads(converted_current["before_json"])["transaction"]["ttype"], "expense")
        self.assertEqual(json.loads(converted_current["after_json"])["transaction"]["ttype"], "transfer_out")

    def test_create_expense_applies_remainder_split(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-04-30",
                "payee": "Test Expense",
                "memo": "phase0 baseline",
                "amount": "12.00",
            },
            splits=[{"envelope_id": 1, "amount": "5.00"}],
            remainder_envelope_id=2,
        )

        db = get_db()
        tx = db.execute(
            "SELECT account_id, ttype, amount_cents, payee, memo FROM transactions WHERE id=?",
            (tx_id,),
        ).fetchone()
        self.assertIsNotNone(tx)
        self.assertEqual(tx["account_id"], 1)
        self.assertEqual(tx["ttype"], "expense")
        self.assertEqual(tx["amount_cents"], -1200)
        self.assertEqual(tx["payee"], "Test Expense")
        self.assertEqual(tx["memo"], "phase0 baseline")

        splits = db.execute(
            "SELECT envelope_id, amount_cents FROM transaction_splits WHERE transaction_id=? ORDER BY envelope_id",
            (tx_id,),
        ).fetchall()
        self.assertEqual([(row["envelope_id"], row["amount_cents"]) for row in splits], [(1, -500), (2, -700)])
        intent = db.execute(
            "SELECT envelope_id, amount_cents FROM transaction_remainder_intents WHERE transaction_id=?",
            (tx_id,),
        ).fetchone()
        self.assertEqual((intent["envelope_id"], intent["amount_cents"]), (2, -700))

    def test_edit_transaction_clears_remainder_intent_when_split_is_explicit(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-04-30",
                "payee": "Remainder Clear",
                "amount": "12.00",
            },
            splits=[{"envelope_id": 1, "amount": "5.00"}],
            remainder_envelope_id=2,
        )

        TransactionsService.edit_transaction(
            tx_id,
            payload={},
            splits=[{"envelope_id": 1, "amount": "12.00"}],
        )

        db = get_db()
        self.assertIsNone(
            db.execute(
                "SELECT 1 FROM transaction_remainder_intents WHERE transaction_id=?",
                (tx_id,),
            ).fetchone()
        )

    def test_edit_transaction_accepts_remainder_only_split_update(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-05-18",
                "payee": "Remainder Only Edit",
                "amount": "12.00",
            },
            splits=[{"envelope_id": 1, "amount": "12.00"}],
        )

        TransactionsService.edit_transaction(
            tx_id,
            payload={},
            splits=None,
            remainder_envelope_id=2,
        )

        db = get_db()
        splits = db.execute(
            "SELECT envelope_id, amount_cents FROM transaction_splits WHERE transaction_id=?",
            (tx_id,),
        ).fetchall()
        self.assertEqual([(row["envelope_id"], row["amount_cents"]) for row in splits], [(2, -1200)])
        intent = db.execute(
            "SELECT envelope_id, amount_cents FROM transaction_remainder_intents WHERE transaction_id=?",
            (tx_id,),
        ).fetchone()
        self.assertEqual((intent["envelope_id"], intent["amount_cents"]), (2, -1200))

    def test_create_transfer_persists_remainder_intent_for_both_legs(self) -> None:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "posted_at": "2026-05-18",
                "amount": "10.00",
                "memo": "two leg remainder",
            },
            out_splits=[{"envelope_id": 1, "amount_cents": -400}],
            in_splits=[{"envelope_id": 1, "amount_cents": 300}],
            out_remainder_envelope_id=2,
            in_remainder_envelope_id=2,
        )

        db = get_db()
        intents = db.execute(
            "SELECT transaction_id, envelope_id, amount_cents FROM transaction_remainder_intents WHERE transaction_id IN (?, ?) ORDER BY transaction_id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in intents],
            [(tx_out_id, 2, -600), (tx_in_id, 2, 700)],
        )

    def test_create_allocation_applies_negative_remainder_split(self) -> None:
        tx_id = TransactionsService.create_allocation(
            payload={
                "account_id": 1,
                "posted_at": "2026-05-16",
                "memo": "allocation signed remainder",
            },
            splits=[{"envelope_id": 1, "amount_cents": 1200}],
            total_cents=1000,
            remainder_envelope_id=2,
        )

        db = get_db()
        splits = db.execute(
            "SELECT envelope_id, amount_cents FROM transaction_splits WHERE transaction_id=? ORDER BY envelope_id",
            (tx_id,),
        ).fetchall()
        self.assertEqual([(row["envelope_id"], row["amount_cents"]) for row in splits], [(1, 1200), (2, -200)])

    def test_create_allocation_rejects_every_invalid_requested_envelope(self) -> None:
        db = get_db()
        archived_id = db.execute(
            "INSERT INTO envelopes(name, locked_account_id, archived_at) VALUES (?, NULL, ?)",
            ("Fictional Archived Allocation", "2026-07-20T00:00:00"),
        ).lastrowid
        db.commit()
        before_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]

        invalid_requests = (
            ([{"envelope_id": 999999, "amount_cents": 0}], None, "does not exist"),
            (
                [
                    {"envelope_id": 6, "amount_cents": 100},
                    {"envelope_id": 1, "amount_cents": -100},
                ],
                None,
                "locked to a different account",
            ),
            ([{"envelope_id": archived_id, "amount_cents": 0}], None, "is archived"),
            ([], 999999, "does not exist"),
            ([], archived_id, "is archived"),
        )

        for splits, remainder_id, expected_message in invalid_requests:
            with self.subTest(expected_message=expected_message, remainder_id=remainder_id):
                with self.assertRaisesRegex(ValueError, expected_message):
                    TransactionsService.create_allocation(
                        payload={
                            "account_id": 1,
                            "posted_at": "2026-07-20",
                            "memo": "Must Not Persist",
                        },
                        splits=splits,
                        total_cents=0,
                        remainder_envelope_id=remainder_id,
                    )

        after_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]
        self.assertEqual(after_count, before_count)

    def test_edit_allocation_rejects_invalid_envelope_and_rolls_back(self) -> None:
        tx_id = TransactionsService.create_allocation(
            payload={
                "account_id": 1,
                "posted_at": "2026-07-20",
                "memo": "Original Fictional Allocation",
            },
            splits=[
                {"envelope_id": 1, "amount_cents": 100},
                {"envelope_id": 2, "amount_cents": -100},
            ],
            total_cents=0,
        )
        db = get_db()
        before_transaction = dict(
            db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        )
        before_splits = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id",
                (tx_id,),
            ).fetchall()
        ]

        invalid_edits = (
            (
                [
                    {"envelope_id": 6, "amount_cents": 100},
                    {"envelope_id": 1, "amount_cents": -100},
                ],
                None,
                "locked to a different account",
            ),
            ([{"envelope_id": 999999, "amount_cents": 0}], None, "does not exist"),
            ([], 999999, "does not exist"),
        )
        for splits, remainder_id, expected_message in invalid_edits:
            with self.subTest(expected_message=expected_message, remainder_id=remainder_id):
                with self.assertRaisesRegex(ValueError, expected_message):
                    TransactionsService.edit_transaction(
                        tx_id,
                        payload={"memo": "Must Not Persist"},
                        splits=splits,
                        remainder_envelope_id=remainder_id,
                    )

        self.assertEqual(
            dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()),
            before_transaction,
        )
        self.assertEqual(
            [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id",
                    (tx_id,),
                ).fetchall()
            ],
            before_splits,
        )

    def test_edit_transaction_updates_ignore_match(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-05-10",
                "payee": "Ignore Match Toggle",
                "amount": "12.00",
            },
            splits=[{"envelope_id": 1, "amount": "12.00"}],
        )

        TransactionsService.edit_transaction(
            tx_id,
            payload={"ignore_match": 1},
            splits=[{"envelope_id": 1, "amount": "12.00"}],
        )
        db = get_db()
        self.assertEqual(
            db.execute("SELECT ignore_match FROM transactions WHERE id=?", (tx_id,)).fetchone()["ignore_match"],
            1,
        )

        TransactionsService.edit_transaction(
            tx_id,
            payload={"ignore_match": 0},
            splits=[{"envelope_id": 1, "amount": "12.00"}],
        )
        self.assertEqual(
            db.execute("SELECT ignore_match FROM transactions WHERE id=?", (tx_id,)).fetchone()["ignore_match"],
            0,
        )

    def test_mark_ignored_transactions_persists_ignore_match_through_edit_path(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-05-10",
                "payee": "Import Ignore",
                "amount": "7.00",
            },
            splits=[{"envelope_id": 1, "amount": "7.00"}],
        )

        marked, failed = mark_ignored_transactions(
            MultiDict([("ignore_tx[]", str(tx_id))]),
            set(),
            TransactionsService.edit_transaction,
        )

        self.assertEqual((marked, failed), (1, 0))
        tx = get_db().execute(
            "SELECT fitid, ignore_match FROM transactions WHERE id=?",
            (tx_id,),
        ).fetchone()
        self.assertEqual(tx["fitid"], f"Ignore-{tx_id}")
        self.assertEqual(tx["ignore_match"], 1)

    def test_create_income_requires_allocated_splits_by_default(self) -> None:
        with self.assertRaises(ValueError):
            TransactionsService.create_income(
                payload={
                    "account_id": 1,
                    "posted_at": "2026-05-10",
                    "payee": "Unallocated Income",
                    "amount": "7.00",
                },
                splits=[],
            )

    def test_create_transfer_creates_linked_pair(self) -> None:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "25.00",
                "posted_at": "2026-04-30",
                "memo": "phase0 transfer baseline",
            },
            out_splits=[{"envelope_id": 1, "amount": "25.00"}],
            in_splits=[{"envelope_id": 6, "amount": "25.00"}],
        )

        db = get_db()
        tx_rows = db.execute(
            "SELECT id, account_id, ttype, amount_cents, xfer_pair_id FROM transactions WHERE id IN (?, ?) ORDER BY id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        self.assertEqual(len(tx_rows), 2)

        tx_by_id = {row["id"]: row for row in tx_rows}
        self.assertEqual(tx_by_id[tx_out_id]["account_id"], 1)
        self.assertEqual(tx_by_id[tx_out_id]["ttype"], "transfer_out")
        self.assertEqual(tx_by_id[tx_out_id]["amount_cents"], -2500)
        self.assertEqual(tx_by_id[tx_out_id]["xfer_pair_id"], tx_in_id)

        self.assertEqual(tx_by_id[tx_in_id]["account_id"], 2)
        self.assertEqual(tx_by_id[tx_in_id]["ttype"], "transfer_in")
        self.assertEqual(tx_by_id[tx_in_id]["amount_cents"], 2500)
        self.assertEqual(tx_by_id[tx_in_id]["xfer_pair_id"], tx_out_id)

        split_rows = db.execute(
            "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, envelope_id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in split_rows],
            [(tx_out_id, 1, -2500), (tx_in_id, 6, 2500)],
        )

    def test_create_transfer_rejects_same_account_without_writing(self) -> None:
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]

        with self.assertRaisesRegex(ValueError, "different source and destination"):
            TransactionsService.create_transfer(
                payload={
                    "from_account_id": 1,
                    "to_account_id": 1,
                    "amount": "25.00",
                    "posted_at": "2026-07-20",
                },
                out_splits=[{"envelope_id": 1, "amount": "25.00"}],
                in_splits=[{"envelope_id": 1, "amount": "25.00"}],
            )

        after_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]
        self.assertEqual(after_count, before_count)

    def test_transfer_route_rejects_same_account_without_writing(self) -> None:
        self._select_user_in_client()
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]

        response = self.client.post(
            "/tx/new/transfer",
            data={
                "from_account_id": "1",
                "to_account_id": "1",
                "amount": "5.00",
                "posted_at": "2026-07-20",
                "transfer_from_1": "5.00",
                "transfer_to_1": "5.00",
            },
            follow_redirects=False,
        )

        after_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]
        self.assertEqual(response.status_code, 302)
        self.assertEqual(after_count, before_count)

    def test_create_expense_rejects_missing_locked_and_archived_envelopes(self) -> None:
        db = get_db()
        archived_id = db.execute(
            "INSERT INTO envelopes(name, locked_account_id, archived_at) VALUES (?, NULL, ?)",
            ("Fictional Archived", "2026-07-20T00:00:00"),
        ).lastrowid
        db.commit()
        before_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]

        invalid_splits = (
            ([{"envelope_id": 999999, "amount": "10.00"}], None, "does not exist"),
            ([{"envelope_id": 999999, "amount": "0"}], 1, "does not exist"),
            ([{"envelope_id": 6, "amount": "10.00"}], None, "locked to a different account"),
            ([{"envelope_id": archived_id, "amount": "10.00"}], None, "is archived"),
            ([{"envelope_id": 1, "amount": "4.00"}], 6, "locked to a different account"),
        )
        for splits, remainder_id, expected_message in invalid_splits:
            with self.subTest(expected_message=expected_message):
                with self.assertRaisesRegex(ValueError, expected_message):
                    TransactionsService.create_expense(
                        payload={
                            "account_id": 1,
                            "posted_at": "2026-07-20",
                            "payee": "Fictional Merchant",
                            "amount": "10.00",
                        },
                        splits=splits,
                        remainder_envelope_id=remainder_id,
                    )

        after_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]
        self.assertEqual(after_count, before_count)

    def test_create_income_rejects_account_incompatible_envelope(self) -> None:
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]

        with self.assertRaisesRegex(ValueError, "locked to a different account"):
            TransactionsService.create_income(
                payload={
                    "account_id": 1,
                    "posted_at": "2026-07-20",
                    "payee": "Fictional Employer",
                    "amount": "10.00",
                },
                splits=[{"envelope_id": 6, "amount": "10.00"}],
            )

        after_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]
        self.assertEqual(after_count, before_count)

    def test_edit_transaction_rejects_incompatible_split_without_partial_update(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-07-20",
                "payee": "Original Fictional Payee",
                "amount": "10.00",
            },
            splits=[{"envelope_id": 1, "amount": "10.00"}],
        )
        db = get_db()

        with self.assertRaisesRegex(ValueError, "locked to a different account"):
            TransactionsService.edit_transaction(
                tx_id,
                payload={"payee": "Must Not Persist"},
                splits=[{"envelope_id": 6, "amount": "10.00"}],
            )

        transaction = db.execute(
            "SELECT payee FROM transactions WHERE id=?",
            (tx_id,),
        ).fetchone()
        split = db.execute(
            "SELECT envelope_id, amount_cents FROM transaction_splits WHERE transaction_id=?",
            (tx_id,),
        ).fetchone()
        self.assertEqual(transaction["payee"], "Original Fictional Payee")
        self.assertEqual((split["envelope_id"], split["amount_cents"]), (1, -1000))

    def test_transfer_create_and_edit_validate_each_leg_atomically(self) -> None:
        db = get_db()
        before_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]
        with self.assertRaisesRegex(ValueError, "locked to a different account"):
            TransactionsService.create_transfer(
                payload={
                    "from_account_id": 1,
                    "to_account_id": 2,
                    "amount": "25.00",
                    "posted_at": "2026-07-20",
                },
                out_splits=[{"envelope_id": 6, "amount": "25.00"}],
                in_splits=[{"envelope_id": 6, "amount": "25.00"}],
            )
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            before_count,
        )

        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "25.00",
                "posted_at": "2026-07-20",
                "memo": "Original Fictional Transfer",
            },
            out_splits=[{"envelope_id": 1, "amount": "25.00"}],
            in_splits=[{"envelope_id": 6, "amount": "25.00"}],
        )
        before_rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM transactions WHERE id IN (?, ?) ORDER BY id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]

        with self.assertRaisesRegex(ValueError, "different source and destination"):
            TransactionsService.edit_transfer(
                tx_out_id,
                payload={
                    "from_account_id": 1,
                    "to_account_id": 1,
                    "amount": "30.00",
                    "posted_at": "2026-07-21",
                    "memo": "Must Not Persist",
                },
                out_splits=[{"envelope_id": 1, "amount": "30.00"}],
                in_splits=[{"envelope_id": 1, "amount": "30.00"}],
            )

        with self.assertRaisesRegex(ValueError, "locked to a different account"):
            TransactionsService.edit_transfer(
                tx_out_id,
                payload={
                    "from_account_id": 1,
                    "to_account_id": 2,
                    "amount": "30.00",
                    "posted_at": "2026-07-21",
                    "memo": "Must Not Persist",
                },
                out_splits=[{"envelope_id": 1, "amount": "30.00"}],
                in_splits=[{"envelope_id": 7, "amount": "30.00"}],
            )

        after_rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM transactions WHERE id IN (?, ?) ORDER BY id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]
        self.assertEqual(after_rows, before_rows)

    def test_transfer_conversion_rejects_incompatible_pair_split_without_writing(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-07-20",
                "payee": "Fictional Conversion",
                "amount": "10.00",
            },
            splits=[{"envelope_id": 1, "amount": "10.00"}],
        )
        db = get_db()
        before_transaction = dict(
            db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        )
        before_count = db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]

        with self.assertRaisesRegex(ValueError, "locked to a different account"):
            TransactionsService.convert_standard_transaction_to_transfer(
                tx_id,
                other_account_id=2,
                current_splits=[{"envelope_id": 1, "amount_cents": -1000}],
                other_splits=[{"envelope_id": 7, "amount_cents": 1000}],
            )

        self.assertEqual(
            db.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"],
            before_count,
        )
        self.assertEqual(
            dict(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()),
            before_transaction,
        )

    def test_create_transfer_accepts_directional_fitids(self) -> None:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "25.00",
                "posted_at": "2026-04-30",
                "memo": "directional fitids",
                "out_fitid": "from-statement-fitid",
                "in_fitid": "to-statement-fitid",
            },
            out_splits=[{"envelope_id": 1, "amount": "25.00"}],
            in_splits=[{"envelope_id": 6, "amount": "25.00"}],
        )

        rows = get_db().execute(
            "SELECT id, fitid FROM transactions WHERE id IN (?, ?) ORDER BY id",
            (tx_out_id, tx_in_id),
        ).fetchall()

        self.assertEqual(
            [(row["id"], row["fitid"]) for row in rows],
            [(tx_out_id, "from-statement-fitid"), (tx_in_id, "to-statement-fitid")],
        )

    def test_edit_transfer_preserves_directional_fitids(self) -> None:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "25.00",
                "posted_at": "2026-04-30",
                "memo": "original transfer",
                "out_fitid": "from-statement-fitid",
                "in_fitid": "to-statement-fitid",
            },
            out_splits=[{"envelope_id": 1, "amount": "25.00"}],
            in_splits=[{"envelope_id": 6, "amount": "25.00"}],
        )

        TransactionsService.edit_transfer(
            tx_out_id,
            payload={
                "from_account_id": 2,
                "to_account_id": 1,
                "amount": "40.00",
                "posted_at": "2026-05-01",
                "memo": "edited transfer",
            },
            out_splits=[{"envelope_id": 6, "amount": "40.00"}],
            in_splits=[{"envelope_id": 1, "amount": "40.00"}],
        )

        rows = get_db().execute(
            "SELECT id, fitid FROM transactions WHERE id IN (?, ?) ORDER BY id",
            (tx_out_id, tx_in_id),
        ).fetchall()

        self.assertEqual(
            [(row["id"], row["fitid"]) for row in rows],
            [(tx_out_id, "from-statement-fitid"), (tx_in_id, "to-statement-fitid")],
        )

    def test_edit_transfer_updates_existing_pair_in_place(self) -> None:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "25.00",
                "posted_at": "2026-04-30",
                "memo": "original transfer",
            },
            out_splits=[{"envelope_id": 1, "amount": "25.00"}],
            in_splits=[{"envelope_id": 6, "amount": "25.00"}],
        )

        edited_out_id, edited_in_id = TransactionsService.edit_transfer(
            tx_out_id,
            payload={
                "from_account_id": 2,
                "to_account_id": 1,
                "amount": "40.00",
                "posted_at": "2026-05-01",
                "memo": "edited transfer",
            },
            out_splits=[{"envelope_id": 6, "amount": "40.00"}],
            in_splits=[{"envelope_id": 1, "amount": "40.00"}],
        )

        self.assertEqual((edited_out_id, edited_in_id), (tx_out_id, tx_in_id))

        db = get_db()
        tx_rows = db.execute(
            "SELECT id, account_id, ttype, amount_cents, posted_at, memo, xfer_pair_id FROM transactions WHERE id IN (?, ?) ORDER BY id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        tx_by_id = {row["id"]: row for row in tx_rows}
        self.assertEqual(tx_by_id[tx_out_id]["account_id"], 2)
        self.assertEqual(tx_by_id[tx_out_id]["ttype"], "transfer_out")
        self.assertEqual(tx_by_id[tx_out_id]["amount_cents"], -4000)
        self.assertEqual(tx_by_id[tx_out_id]["posted_at"], "2026-05-01")
        self.assertEqual(tx_by_id[tx_out_id]["memo"], "edited transfer")
        self.assertEqual(tx_by_id[tx_out_id]["xfer_pair_id"], tx_in_id)

        self.assertEqual(tx_by_id[tx_in_id]["account_id"], 1)
        self.assertEqual(tx_by_id[tx_in_id]["ttype"], "transfer_in")
        self.assertEqual(tx_by_id[tx_in_id]["amount_cents"], 4000)
        self.assertEqual(tx_by_id[tx_in_id]["xfer_pair_id"], tx_out_id)

        split_rows = db.execute(
            "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, envelope_id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in split_rows],
            [(tx_out_id, 6, -4000), (tx_in_id, 1, 4000)],
        )

    def test_edit_transfer_rolls_back_if_split_rewrite_fails(self) -> None:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "25.00",
                "posted_at": "2026-04-30",
                "memo": "original transfer",
            },
            out_splits=[{"envelope_id": 1, "amount": "25.00"}],
            in_splits=[{"envelope_id": 6, "amount": "25.00"}],
        )

        db = get_db()
        before_transactions = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM transactions WHERE id IN (?, ?) ORDER BY id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]
        before_splits = [
            dict(row)
            for row in db.execute(
                "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, envelope_id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]

        with patch(
            "app.services.transactions_service.splits_repo.insert_split",
            side_effect=RuntimeError("simulated split insert failure"),
        ):
            with self.assertRaises(RuntimeError):
                TransactionsService.edit_transfer(
                    tx_out_id,
                    payload={
                        "from_account_id": 2,
                        "to_account_id": 1,
                        "amount": "40.00",
                        "posted_at": "2026-05-01",
                        "memo": "should roll back",
                    },
                    out_splits=[{"envelope_id": 6, "amount": "40.00"}],
                    in_splits=[{"envelope_id": 1, "amount": "40.00"}],
                )

        after_transactions = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM transactions WHERE id IN (?, ?) ORDER BY id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]
        after_splits = [
            dict(row)
            for row in db.execute(
                "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, envelope_id",
                (tx_out_id, tx_in_id),
            ).fetchall()
        ]

        self.assertEqual(after_transactions, before_transactions)
        self.assertEqual(after_splits, before_splits)


    def test_create_income_preserves_mixed_signed_splits(self) -> None:
        tx_id = TransactionsService.create_income(
            payload={
                "account_id": 1,
                "posted_at": "2026-05-16",
                "payee": "Mixed Signed Income",
                "amount": "300.19",
            },
            splits=[
                {"envelope_id": 1, "amount": "370.19"},
                {"envelope_id": 2, "amount": "-70.00"},
            ],
        )

        rows = get_db().execute(
            "SELECT envelope_id, amount_cents FROM transaction_splits WHERE transaction_id=? ORDER BY envelope_id",
            (tx_id,),
        ).fetchall()
        self.assertEqual([(row["envelope_id"], row["amount_cents"]) for row in rows], [(1, 37019), (2, -7000)])

    def test_create_expense_keeps_positive_entry_compatibility(self) -> None:
        tx_id = TransactionsService.create_expense(
            payload={
                "account_id": 1,
                "posted_at": "2026-05-16",
                "payee": "Positive Entry Expense",
                "amount": "30.00",
            },
            splits=[{"envelope_id": 1, "amount": "20.00"}, {"envelope_id": 2, "amount": "10.00"}],
        )

        rows = get_db().execute(
            "SELECT envelope_id, amount_cents FROM transaction_splits WHERE transaction_id=? ORDER BY envelope_id",
            (tx_id,),
        ).fetchall()
        self.assertEqual([(row["envelope_id"], row["amount_cents"]) for row in rows], [(1, -2000), (2, -1000)])

    def test_create_transfer_in_leg_preserves_mixed_signed_splits(self) -> None:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload={
                "from_account_id": 1,
                "to_account_id": 2,
                "amount": "300.19",
                "posted_at": "2026-05-16",
                "memo": "mixed signed transfer",
            },
            out_splits=[{"envelope_id": 1, "amount": "300.19"}],
            in_splits=[
                {"envelope_id": 6, "amount": "370.19"},
                {"envelope_id": 2, "amount": "-70.00"},
            ],
        )

        rows = get_db().execute(
            "SELECT transaction_id, envelope_id, amount_cents FROM transaction_splits WHERE transaction_id IN (?, ?) ORDER BY transaction_id, envelope_id",
            (tx_out_id, tx_in_id),
        ).fetchall()
        self.assertEqual(
            [(row["transaction_id"], row["envelope_id"], row["amount_cents"]) for row in rows],
            [(tx_out_id, 1, -30019), (tx_in_id, 2, -7000), (tx_in_id, 6, 37019)],
        )
