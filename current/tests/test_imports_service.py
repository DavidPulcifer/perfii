import hashlib
from io import BytesIO
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from werkzeug.datastructures import MultiDict

from app.services.imports_service import (
    CsvColumnMappingRequired,
    apply_csv_polarity,
    already_imported_transfer_match_indexes,
    auto_match_suggestions,
    build_import_row_states,
    combine_qfx_payee_and_memo,
    detect_csv_credit_card_polarity,
    find_account_for_import,
    find_account_for_import_source,
    import_account_by_id,
    import_row_fingerprint,
    import_row_provenance_indexes,
    import_account_for_review,
    imported_fitid_details,
    imported_fitids_request_response,
    imported_fitids_response,
    import_review_account_id,
    import_upload_context,
    import_review_context,
    import_review_existing_fitids,
    import_prefills_for_import_review,
    import_match_score,
    import_transaction_amount_cents,
    manual_import_candidates_request_response,
    manual_import_candidates_response,
    manual_candidate_date_from,
    manual_import_candidate_items,
    manual_import_rows_from_request_args,
    parse_csv,
    parse_qfx,
    parse_statement_upload,
    parse_uploaded_statement_file,
)
from tests.helpers import FinanceAppTestCase


class ImportAutoMatchTests(TestCase):
    def test_import_match_score_prefers_exact_amount_payee_and_date(self) -> None:
        score = import_match_score(
            {"index": 0, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop", "memo": "Downtown"},
            {"id": 10, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop", "memo": "Downtown"},
        )

        self.assertIsNotNone(score)
        self.assertGreater(score, 95)

    def test_import_match_score_rejects_outside_date_window(self) -> None:
        self.assertIsNone(import_match_score(
            {"index": 0, "posted_at": "2026-05-20", "amount_cents": -1299, "payee": "Coffee Shop"},
            {"id": 10, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"},
        ))

    def test_import_match_score_rejects_opposite_sign(self) -> None:
        self.assertIsNone(import_match_score(
            {"index": 0, "posted_at": "2026-05-03", "amount_cents": 1299, "payee": "Coffee Shop"},
            {"id": 10, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"},
        ))

    def test_auto_match_suggestions_leaves_tied_candidates_blank(self) -> None:
        suggestions = auto_match_suggestions(
            [
                {"index": 0, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"},
                {"index": 1, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"},
            ],
            [{"id": 10, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"}],
        )

        self.assertEqual(suggestions, {})

    def test_auto_match_suggestions_rejects_weak_similarity(self) -> None:
        suggestions = auto_match_suggestions(
            [{"index": 0, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"}],
            [{"id": 10, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Hardware Depot"}],
        )

        self.assertEqual(suggestions, {})

    def test_auto_match_suggestions_matches_transfer_payment_by_amount_and_date(self) -> None:
        suggestions = auto_match_suggestions(
            [{"index": 4, "posted_at": "2026-03-24", "amount_cents": 700, "payee": "Payment Thank You - Web"}],
            [{
                "id": 372,
                "posted_at": "2026-03-25",
                "amount_cents": 700,
                "payee": "Example Bank - 0202 / 0404",
                "ttype": "transfer_in",
                "xfer_pair_id": 371,
            }],
        )

        self.assertEqual(suggestions, {372: 4})

    def test_import_match_score_requires_payment_text_for_transfer_bonus(self) -> None:
        self.assertIsNone(import_match_score(
            {"index": 0, "posted_at": "2026-03-24", "amount_cents": 700, "payee": "Coffee Shop"},
            {
                "id": 372,
                "posted_at": "2026-03-25",
                "amount_cents": 700,
                "payee": "Example Bank - 0202 / 0404",
                "ttype": "transfer_in",
                "xfer_pair_id": 371,
            },
        ))

    def test_auto_match_suggestions_skips_duplicate_fitid_import_rows(self) -> None:
        suggestions = auto_match_suggestions(
            [{"index": 0, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop", "fitid": "fit-1"}],
            [{"id": 10, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"}],
            existing_fitids={"fit-1"},
        )

        self.assertEqual(suggestions, {})


    def test_already_imported_transfer_match_indexes_flags_statement_payment_with_different_fitid(self) -> None:
        rows = [
            {
                "id": 1065,
                "posted_at": "2026-02-20",
                "amount_cents": "85000",
                "payee": "Example Bank - 0202",
                "memo": None,
                "fitid": "DEMO-TRANSFER-A",
                "ttype": "transfer_in",
                "xfer_pair_id": 1064,
                "import_validated": True,
            },
            {
                "id": 1031,
                "posted_at": "2026-01-23",
                "amount_cents": "18100",
                "payee": "Example Bank - 0202",
                "memo": None,
                "fitid": "DEMO-TRANSFER-B",
                "ttype": "transfer_in",
                "xfer_pair_id": 1030,
                "import_validated": True,
            },
        ]

        matches = already_imported_transfer_match_indexes(
            [
                {
                    "index": 19,
                    "posted_at": "2026-02-17",
                    "amount_cents": 85000,
                    "payee": "Payment Thank You - Web",
                    "fitid": "DEMO-STATEMENT-A",
                },
                {
                    "index": 156,
                    "posted_at": "2026-01-22",
                    "amount_cents": 18100,
                    "payee": "Payment Thank You - Web",
                    "fitid": "DEMO-STATEMENT-B",
                },
            ],
            8,
            list_transactions_func=lambda **kwargs: (rows, len(rows)),
            get_transaction_func=lambda tx_id: {1064: {"account_id": 1}, 1030: {"account_id": 1}}.get(tx_id),
        )

        self.assertEqual(matches, {19, 156})

    def test_already_imported_transfer_match_indexes_flags_counterparty_account_text(self) -> None:
        rows = [
            {
                "id": 201,
                "posted_at": "2026-04-08",
                "amount_cents": "-38000",
                "payee": "Example Bank - 0202",
                "memo": "EXAMPLE TRANSFER TRACE",
                "fitid": "DEMO-COUNTERPARTY-A",
                "ttype": "transfer_out",
                "xfer_pair_id": 202,
                "import_validated": True,
            },
            {
                "id": 251,
                "posted_at": "2026-04-15",
                "amount_cents": "-14000",
                "payee": "Example Bank - 0202",
                "memo": "Oil Change",
                "fitid": "DEMO-COUNTERPARTY-B",
                "ttype": "transfer_out",
                "xfer_pair_id": 252,
                "import_validated": True,
            },
            {
                "id": 255,
                "posted_at": "2026-10-04",
                "amount_cents": "-25600",
                "payee": "Example Bank - 0202",
                "memo": "EXAMPLE TRANSFER TRACE",
                "fitid": "DEMO-COUNTERPARTY-C",
                "ttype": "transfer_out",
                "xfer_pair_id": 256,
                "import_validated": True,
            },
        ]

        matches = already_imported_transfer_match_indexes(
            [
                {
                    "index": 17,
                    "posted_at": "2026-10-04",
                    "amount_cents": -25600,
                    "payee": "Example Bank (Account ****0202)",
                    "memo": "Withdrawal",
                },
                {
                    "index": 41,
                    "posted_at": "2026-04-13",
                    "amount_cents": -14000,
                    "payee": "Example Bank (Account ****0202)",
                    "memo": "Withdrawal",
                },
                {
                    "index": 42,
                    "posted_at": "2026-04-08",
                    "amount_cents": -38000,
                    "payee": "Example Bank (Account ****0202)",
                    "memo": "Withdrawal",
                },
            ],
            2,
            list_transactions_func=lambda **kwargs: (rows, len(rows)),
            get_transaction_func=lambda tx_id: {202: {"account_id": 1}, 252: {"account_id": 1}, 256: {"account_id": 1}}.get(tx_id),
        )

        self.assertEqual(matches, {17, 41, 42})

    def test_already_imported_transfer_match_indexes_requires_counterparty_clue(self) -> None:
        matches = already_imported_transfer_match_indexes(
            [{"index": 3, "posted_at": "2026-04-08", "amount_cents": -38000, "payee": "Withdrawal"}],
            2,
            list_transactions_func=lambda **kwargs: ([{
                "id": 201,
                "posted_at": "2026-04-08",
                "amount_cents": "-38000",
                "payee": "Example Bank - 0202",
                "memo": "EXAMPLE TRANSFER TRACE",
                "fitid": "DEMO-COUNTERPARTY-A",
                "ttype": "transfer_out",
                "xfer_pair_id": 202,
            }], 1),
            get_transaction_func=lambda tx_id: {"account_id": 1},
        )

        self.assertEqual(matches, set())

    def test_already_imported_transfer_match_indexes_requires_account_side_validation(self) -> None:
        matches = already_imported_transfer_match_indexes(
            [{"index": 4, "posted_at": "2026-03-24", "amount_cents": 700, "payee": "Payment Thank You - Web"}],
            8,
            list_transactions_func=lambda **kwargs: ([{
                "id": 372,
                "posted_at": "2026-03-25",
                "amount_cents": "700",
                "payee": "Example Bank - 0202 / 0404",
                "memo": None,
                "ttype": "transfer_in",
                "xfer_pair_id": 371,
                "import_validated": False,
            }], 1),
            get_transaction_func=lambda tx_id: {"account_id": 1},
        )

        self.assertEqual(matches, set())

    def test_already_imported_transfer_match_indexes_keeps_ambiguous_counterparty_matches_blank(self) -> None:
        rows = [
            {
                "id": 201,
                "posted_at": "2026-04-08",
                "amount_cents": "-38000",
                "payee": "Example Bank - 0202",
                "memo": None,
                "fitid": "DEMO-AMBIGUOUS-A",
                "ttype": "transfer_out",
                "xfer_pair_id": 202,
            },
            {
                "id": 301,
                "posted_at": "2026-04-08",
                "amount_cents": "-38000",
                "payee": "Example Bank - 0202",
                "memo": None,
                "fitid": "DEMO-AMBIGUOUS-B",
                "ttype": "transfer_out",
                "xfer_pair_id": 302,
            },
        ]

        matches = already_imported_transfer_match_indexes(
            [{"index": 42, "posted_at": "2026-04-08", "amount_cents": -38000, "payee": "Example Bank (Account ****0202)", "memo": "Withdrawal"}],
            2,
            list_transactions_func=lambda **kwargs: (rows, len(rows)),
            get_transaction_func=lambda tx_id: {"account_id": 1},
        )

        self.assertEqual(matches, set())

    def test_already_imported_transfer_match_indexes_does_not_flag_plain_income(self) -> None:
        matches = already_imported_transfer_match_indexes(
            [{"index": 4, "posted_at": "2026-02-17", "amount_cents": 85000, "payee": "Payment Thank You - Web"}],
            8,
            list_transactions_func=lambda **kwargs: ([{
                "id": 9,
                "posted_at": "2026-02-17",
                "amount_cents": "85000",
                "payee": "Payment Thank You - Web",
                "memo": None,
                "fitid": "manual-income",
                "ttype": "income",
            }], 1),
            get_transaction_func=lambda tx_id: None,
        )

        self.assertEqual(matches, set())


    def test_import_row_provenance_indexes_match_without_fitid(self) -> None:
        rows = [{"posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop", "memo": "Latte", "fitid": "statement-fitid"}]
        fp = import_row_fingerprint(rows[0], account_id=7, source_bankid="BANK", source_acctid="ACCT")

        matches = import_row_provenance_indexes(
            rows,
            7,
            source_bankid="BANK",
            source_acctid="ACCT",
            list_import_provenance_matches_func=lambda account_id, fingerprints, **kwargs: [
                {"row_fingerprint": fp, "match_type": "created"}
            ],
        )

        self.assertEqual(matches, {0})

    def test_imported_fitids_response_includes_provenance_row_indexes_before_fuzzy_fallback(self) -> None:
        row = {"index": 2, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop", "memo": "Latte"}
        fp = import_row_fingerprint(row, account_id=7, source_bankid="BANK", source_acctid="ACCT")

        response = imported_fitids_response(
            7,
            list_imported_fitid_rows_func=lambda account_id: [],
            import_rows=[row],
            list_import_provenance_matches_func=lambda account_id, fingerprints, **kwargs: [
                {"row_fingerprint": fp, "match_type": "manual_match"}
            ],
            source_bankid="BANK",
            source_acctid="ACCT",
        )

        self.assertEqual(response["row_indexes"], [2])

    def test_manual_import_rows_from_request_args_parses_json_list(self) -> None:
        rows = manual_import_rows_from_request_args(MultiDict({
            "imports": '[{"index": 0, "amount_cents": -100}]',
        }))

        self.assertEqual(rows, [{"index": 0, "amount_cents": -100}])
        self.assertEqual(manual_import_rows_from_request_args(MultiDict({"imports": "not-json"})), [])

    def test_manual_import_candidates_response_includes_auto_match_suggestions(self) -> None:
        def list_transactions(**kwargs):
            return ([
                {"id": 10, "posted_at": "2026-05-03", "amount_cents": "-1299", "payee": "Coffee Shop", "memo": None, "ttype": "expense"},
            ], 1)

        response = manual_import_candidates_response(
            100,
            30,
            list_transactions_func=list_transactions,
            get_transaction_func=lambda tx_id: None,
            import_rows=[
                {"index": 2, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"},
            ],
        )

        self.assertEqual(response["items"][0]["id"], 10)
        self.assertEqual(response["items"][0]["suggested_import_index"], 2)

    def test_manual_import_candidates_response_groups_out_of_window_candidates(self) -> None:
        def list_transactions(**kwargs):
            return ([
                {"id": 10, "posted_at": "2026-05-03", "amount_cents": "-1299", "payee": "Coffee Shop", "memo": None, "ttype": "expense"},
                {"id": 11, "posted_at": "2026-02-01", "amount_cents": "-4500", "payee": "Old Manual", "memo": None, "ttype": "expense"},
            ], 2)

        response = manual_import_candidates_response(
            100,
            3650,
            list_transactions_func=list_transactions,
            get_transaction_func=lambda tx_id: None,
            import_rows=[
                {"index": 2, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop"},
            ],
        )

        self.assertEqual([item["id"] for item in response["items"]], [10])
        self.assertEqual([item["id"] for item in response["overflow_items"]], [11])

    def test_manual_import_candidates_request_response_uses_fitids_for_duplicate_suggestions(self) -> None:
        response = manual_import_candidates_request_response(
            MultiDict({
                "account_id": "42",
                "imports": '[{"index": 0, "posted_at": "2026-05-03", "amount_cents": -1299, "payee": "Coffee Shop", "fitid": "fit-1"}]',
            }),
            list_transactions_func=lambda **kwargs: ([
                {"id": 10, "posted_at": "2026-05-03", "amount_cents": "-1299", "payee": "Coffee Shop", "memo": None, "ttype": "expense"},
            ], 1),
            get_transaction_func=lambda tx_id: None,
            list_imported_fitid_rows_func=lambda account_id: [{"fitid": "fit-1", "payee": "Coffee Shop", "memo": None}],
        )

        self.assertIsNone(response["items"][0]["suggested_import_index"])


class ImportsServiceTests(FinanceAppTestCase):
    def test_find_account_for_import_prefers_exact_then_partial_identifier_matches(self) -> None:
        accounts = [
            {"id": 1, "bankid": "111", "acctid": "aaa"},
            {"id": 2, "bankid": "222", "acctid": "bbb"},
            {"id": 3, "bankid": "", "acctid": ""},
        ]

        self.assertEqual(find_account_for_import(accounts, {"bankid": "222", "acctid": "bbb"})["id"], 2)
        self.assertEqual(find_account_for_import(accounts, {"bankid": "999", "acctid": "aaa"})["id"], 1)
        self.assertEqual(find_account_for_import(accounts, {"bankid": "222", "acctid": ""})["id"], 2)
        self.assertIsNone(find_account_for_import(accounts, {"bankid": "222", "acctid": "zzz"}))

    def test_find_account_for_import_prefers_suffix_over_bankid_only_match(self) -> None:
        accounts = [
            {"id": 1, "name": "Example Bank - 0202", "bankid": "EXAMPLE-BANK", "acctid": "DEMO-ACCT-0202", "acct_key": "acct:example-bank-0202"},
            {"id": 3, "name": "Example Bank - 0101", "bankid": "", "acctid": "", "acct_key": "acct:example-bank-0101"},
        ]

        self.assertEqual(find_account_for_import(accounts, {"bankid": "EXAMPLE-BANK", "acctid": "DEMO-ACCT-0101"})["id"], 3)

    def test_find_account_for_import_requires_unique_bankid_only_match(self) -> None:
        accounts = [
            {"id": 1, "name": "Example Bank - 0202", "bankid": "EXAMPLE-BANK", "acctid": "DEMO-ACCT-0202", "acct_key": "acct:example-bank-0202"},
            {"id": 3, "name": "Example Bank Backup", "bankid": "EXAMPLE-BANK", "acctid": "", "acct_key": "acct:example-bank-backup"},
        ]

        self.assertIsNone(find_account_for_import(accounts, {"bankid": "EXAMPLE-BANK", "acctid": "DEMO-UNKNOWN-9999"}))

    def test_find_account_for_import_matches_unique_unidentified_account_suffix(self) -> None:
        accounts = [
            {"id": 5, "name": "Example Investing", "bankid": "", "acctid": "", "acct_key": "acct:example-investing"},
            {"id": 6, "name": "Example Bank - 0404", "bankid": "", "acctid": "", "acct_key": "acct:example-bank-0404"},
            {"id": 8, "name": "Sample Credit Union - 0303", "bankid": "", "acctid": "", "acct_key": "acct:sample-credit-union-0303"},
        ]

        self.assertEqual(find_account_for_import(accounts, {"bankid": "", "acctid": "DEMO-ACCT-0404"})["id"], 6)

    def test_find_account_for_import_suffix_fallback_requires_unique_match(self) -> None:
        accounts = [
            {"id": 5, "name": "Example Investing", "bankid": "", "acctid": "", "acct_key": "acct:example-investing"},
            {"id": 6, "name": "Example Bank - 0404", "bankid": "", "acctid": "", "acct_key": "acct:example-bank-0404"},
            {"id": 16, "name": "Backup 0404", "bankid": "", "acctid": "", "acct_key": "acct:backup-0404"},
        ]

        self.assertIsNone(find_account_for_import(accounts, {"bankid": "", "acctid": "DEMO-ACCT-0404"}))

    def test_find_account_for_import_returns_none_without_confident_match(self) -> None:
        accounts = [
            {"id": 1, "bankid": "111", "acctid": "aaa"},
            {"id": 2, "bankid": "", "acctid": ""},
        ]

        self.assertIsNone(find_account_for_import(accounts, {"bankid": "999", "acctid": "zzz"}))
        self.assertIsNone(find_account_for_import(accounts[:1], {"bankid": "999", "acctid": "zzz"}))
        self.assertIsNone(find_account_for_import([], {"bankid": "999", "acctid": "zzz"}))

    def test_find_account_for_import_matches_example_investing_csv_filename(self) -> None:
        accounts = [
            {"id": 1, "name": "Sample Credit Union Checking", "bankid": "", "acctid": "", "acct_key": "acct:sample-checking"},
            {"id": 2, "name": "Example Investing Cash Account", "bankid": "", "acctid": "", "acct_key": "acct:example-investing-cash"},
        ]
        parsed = {"bankid": None, "acctid": None, "_source_type": "csv", "_source_filename": "Example_Investing_Transactions_2026-06-01.csv"}

        self.assertEqual(find_account_for_import(accounts, parsed)["id"], 2)
        self.assertEqual(find_account_for_import_source(accounts, parsed), "filename")

    def test_find_account_for_import_filename_match_requires_unique_account(self) -> None:
        accounts = [
            {"id": 1, "name": "Example Investing Cash", "bankid": "", "acctid": "", "acct_key": "acct:example-investing-cash"},
            {"id": 2, "name": "Example Investing Brokerage", "bankid": "", "acctid": "", "acct_key": "acct:example-investing-brokerage"},
        ]
        parsed = {"bankid": None, "acctid": None, "_source_type": "csv", "_source_filename": "example-investing-transactions.csv"}

        self.assertIsNone(find_account_for_import(accounts, parsed))
        self.assertIsNone(find_account_for_import_source(accounts, parsed))

    def test_find_account_for_import_filename_uses_suffix_and_normalized_names(self) -> None:
        accounts = [
            {"id": 1, "name": "Example Bank Checking 0404", "bankid": "", "acctid": "", "acct_key": "acct:example-bank-checking-0404"},
            {"id": 2, "name": "Sample Rewards Card", "bankid": "", "acctid": "", "acct_key": "acct:sample-rewards-card"},
        ]

        self.assertEqual(find_account_for_import(accounts, {"bankid": None, "acctid": None, "_source_type": "csv", "_source_filename": "transactions_0404.csv"})["id"], 1)
        self.assertEqual(find_account_for_import(accounts, {"bankid": None, "acctid": None, "_source_type": "csv", "_source_filename": "sample-rewards-card-export.csv"})["id"], 2)

    def test_find_account_for_import_strong_identifiers_win_over_filename(self) -> None:
        accounts = [
            {"id": 1, "name": "Example Investing Cash", "bankid": "", "acctid": "DEMO-CASH", "acct_key": "acct:example-investing"},
            {"id": 2, "name": "Example Bank Checking", "bankid": "", "acctid": "DEMO-CHECKING", "acct_key": "acct:example-bank"},
        ]
        parsed = {"bankid": "", "acctid": "DEMO-CHECKING", "_source_type": "csv", "_source_filename": "example-investing-transactions.csv"}

        self.assertEqual(find_account_for_import(accounts, parsed)["id"], 2)
        self.assertEqual(find_account_for_import_source(accounts, parsed), "identifier")

    def test_find_account_for_import_ignores_non_csv_filename(self) -> None:
        accounts = [
            {"id": 1, "name": "Example Investing Cash", "bankid": "", "acctid": "", "acct_key": "acct:example-investing"},
        ]
        parsed = {"bankid": None, "acctid": None, "_source_type": "qfx", "_source_filename": "example-investing.qfx"}

        self.assertIsNone(find_account_for_import(accounts, parsed))

    def test_import_account_for_review_uses_manual_selection_before_detection(self) -> None:
        accounts = [
            {"id": 1, "bankid": "111", "acctid": "aaa"},
            {"id": "2", "bankid": "", "acctid": ""},
        ]

        self.assertEqual(import_account_by_id(accounts, "2")["id"], "2")
        self.assertEqual(import_account_for_review(accounts, {"bankid": "999", "acctid": "zzz"}, 2)["id"], "2")
        self.assertIsNone(import_account_by_id(accounts, "not-an-id"))

    def test_import_upload_context_loads_accounts_for_template(self) -> None:
        accounts = [{"id": 1, "name": "Checking"}]

        self.assertEqual(import_upload_context(list_accounts_func=lambda: accounts), {
            "accounts": accounts,
            "selected_account_id": None,
            "account_detection_message": None,
        })

    def test_import_review_account_helpers_normalize_account_and_fitids(self) -> None:
        calls = []

        def list_fitids(account_id):
            calls.append(account_id)
            return ["fit-1", "fit-2"]

        self.assertEqual(import_review_account_id({"id": "42"}), 42)
        self.assertIsNone(import_review_account_id(None))
        self.assertIsNone(import_review_account_id({"id": "bad"}))
        self.assertEqual(import_review_existing_fitids({"id": "42"}, list_fitids), {"fit-1", "fit-2"})
        self.assertEqual(calls, [42])
        self.assertEqual(import_review_existing_fitids(None, list_fitids), set())
        self.assertEqual(calls, [42])

    def test_parse_uploaded_statement_file_validates_and_parses_upload(self) -> None:
        class Upload(BytesIO):
            def __init__(self, data: bytes, filename: str):
                super().__init__(data)
                self.filename = filename

        result = parse_uploaded_statement_file(
            Upload(b"statement-data", " statement.csv "),
            parse_func=lambda data, filename: {"data": data, "filename": filename},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.parsed, {"data": b"statement-data", "filename": "statement.csv"})
        self.assertIsNone(result.error_message)

    def test_parse_uploaded_statement_file_reports_missing_empty_and_parse_errors(self) -> None:
        class Upload(BytesIO):
            def __init__(self, data: bytes, filename: str | None):
                super().__init__(data)
                self.filename = filename

        missing = parse_uploaded_statement_file(None)
        empty = parse_uploaded_statement_file(Upload(b"", "empty.csv"))

        self.assertEqual(missing.error_message, "Please choose a QFX/OFX/CSV file.")
        self.assertEqual(missing.flash_category, "warning")
        self.assertEqual(empty.error_message, "Uploaded file is empty.")
        self.assertEqual(empty.flash_category, "warning")
        result = parse_uploaded_statement_file(
            Upload(b"bad-data", "bad.csv"),
            parse_func=lambda data, filename: (_ for _ in ()).throw(ValueError("bad parse")),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_message, "Could not parse file: bad parse")
        self.assertEqual(result.flash_category, "danger")

    def test_import_review_context_uses_selected_account_when_detection_is_unknown(self) -> None:
        parsed = {"bankid": "999", "acctid": "zzz", "transactions": [{"amount_cents": -100}]}
        accounts = [
            {"id": 1, "bankid": "111", "acctid": "aaa"},
            {"id": "2", "bankid": "", "acctid": ""},
        ]
        calls = []

        context = import_review_context(
            parsed,
            list_accounts_func=lambda: accounts,
            list_fitids_func=lambda account_id: calls.append(("fitids", account_id)) or [],
            list_envelopes_func=lambda: [],
            selected_account_id=2,
            import_prefills_func=lambda transactions, account_id, existing_fitids: calls.append(("prefills", account_id)) or [],
            payee_prefills_func=lambda transactions, account_id: calls.append(("payee_prefills", account_id)) or [],
        )

        self.assertEqual(context["acct"], accounts[1])
        self.assertEqual(calls, [("fitids", 2), ("prefills", 2), ("payee_prefills", 2)])

    def test_import_review_context_builds_template_payload(self) -> None:
        parsed = {"bankid": "222", "acctid": "bbb", "file_hash": "hash-1", "_source_type": "qfx", "_source_filename": "statement.qfx", "transactions": [{"amount_cents": -100}]}
        accounts = [
            {"id": 1, "bankid": "111", "acctid": "aaa"},
            {"id": "2", "bankid": "222", "acctid": "bbb"},
        ]
        calls = []

        context = import_review_context(
            parsed,
            list_accounts_func=lambda: accounts,
            list_fitids_func=lambda account_id: calls.append(("fitids", account_id)) or ["fit-1"],
            list_envelopes_func=lambda: calls.append(("envelopes",)) or [{"id": 9}],
            import_prefills_func=lambda transactions, account_id, existing_fitids: calls.append(("prefills", transactions, account_id, existing_fitids)) or [{"row_index": 0, "prefill": False}],
            payee_prefills_func=lambda transactions, account_id: calls.append(("payee_prefills", transactions, account_id)) or [{"row_index": 0, "payee_prefill": True, "canonical_payee": "Clean Payee"}],
            create_import_review_source_func=lambda **kwargs: calls.append(("source", kwargs)) or {"token": "opaque-source-token"},
            cleanup_import_review_sources_func=lambda: calls.append(("source_cleanup",)),
        )

        self.assertIs(context["parsed"], parsed)
        self.assertEqual(context["accounts"], accounts)
        self.assertEqual(context["acct"], accounts[1])
        self.assertEqual(context["envelopes_all"], [{"id": 9}])
        self.assertEqual(context["balances_json"], {})
        self.assertNotIn("exp_suggestions", context)
        self.assertEqual(context["import_prefills"], [{"row_index": 0, "prefill": False}])
        self.assertEqual(context["payee_prefills"], [{"row_index": 0, "payee_prefill": True, "canonical_payee": "Clean Payee"}])
        self.assertEqual(context["import_source_token"], "opaque-source-token")
        self.assertIn(("source_cleanup",), calls)
        self.assertIn(("source", {
            "account_id": 2,
            "source_bankid": "222",
            "source_acctid": "bbb",
            "file_hash": "hash-1",
            "source_type": "qfx",
            "source_filename": "statement.qfx",
        }), calls)
        self.assertEqual(context["existing_fitids"], {"fit-1"})
        self.assertTrue(context["show_missing_fitid_badges"])
        self.assertEqual(calls[:4], [
            ("fitids", 2),
            ("envelopes",),
            ("prefills", parsed["transactions"], 2, {"fit-1"}),
            ("payee_prefills", parsed["transactions"], 2),
        ])

    def test_import_review_context_manual_rules_override_automatic_predictions(self) -> None:
        parsed = {
            "bankid": "222",
            "acctid": "bbb",
            "transactions": [
                {"amount_cents": -100, "payee": "Manual Import"},
                {"amount_cents": -200, "payee": "Manual Payee"},
                {"amount_cents": -300, "payee": "Automatic"},
            ],
        }
        accounts = [{"id": "2", "bankid": "222", "acctid": "bbb"}]

        context = import_review_context(
            parsed,
            list_accounts_func=lambda: accounts,
            list_fitids_func=lambda account_id: [],
            list_envelopes_func=lambda: [],
            import_prefills_func=lambda transactions, account_id, existing_fitids, row_states=None: [
                {"row_index": 0, "prefill": True, "single_envelope_id": 99, "prediction_type": "learned"},
                {"row_index": 2, "prefill": True, "single_envelope_id": 77, "prediction_type": "learned"},
            ],
            payee_prefills_func=lambda transactions, account_id: [
                {"row_index": 1, "payee_prefill": True, "canonical_payee": "Automatic Payee"},
                {"row_index": 2, "payee_prefill": True, "canonical_payee": "Automatic Only"},
            ],
            rule_prefills_func=lambda transactions, account_id, **kwargs: {
                "import_prefills": [
                    {
                        "row_index": 0,
                        "prefill": False,
                        "prediction_type": "manual_rule",
                        "debug_reason_codes": ["manual_rule_conflict"],
                    },
                ],
                "payee_prefills": [
                    {
                        "row_index": 1,
                        "payee_prefill": True,
                        "canonical_payee": "Manual Payee",
                        "rule_ids": [12],
                    },
                ],
            },
        )

        self.assertEqual(context["import_prefills"], [
            {
                "row_index": 0,
                "prefill": False,
                "prediction_type": "manual_rule",
                "debug_reason_codes": ["manual_rule_conflict"],
            },
            {"row_index": 2, "prefill": True, "single_envelope_id": 77, "prediction_type": "learned"},
        ])
        self.assertEqual(context["payee_prefills"], [
            {
                "row_index": 1,
                "payee_prefill": True,
                "canonical_payee": "Manual Payee",
                "rule_ids": [12],
            },
            {"row_index": 2, "payee_prefill": True, "canonical_payee": "Automatic Only"},
        ])

    def test_import_review_context_includes_account_envelope_balances(self) -> None:
        parsed = {"bankid": "222", "acctid": "bbb", "transactions": [{"amount_cents": -100}]}
        accounts = [{"id": "2", "bankid": "222", "acctid": "bbb"}]

        context = import_review_context(
            parsed,
            list_accounts_func=lambda: accounts,
            list_fitids_func=lambda account_id: [],
            list_envelopes_func=lambda: [{"id": 9}, {"id": 10}],
            account_envelope_balances_func=lambda: {
                (2, 9): 12345,
                (2, 10): 0,
                (7, 9): -500,
            },
            import_prefills_func=lambda transactions, account_id, existing_fitids: [],
            payee_prefills_func=lambda transactions, account_id: [],
        )

        self.assertEqual(context["balances_json"], {
            "2": {"9": 12345, "10": 0},
            "7": {"9": -500},
        })

    def test_import_review_context_hides_missing_fitid_badges_for_csv_imports(self) -> None:
        base_kwargs = {
            "list_accounts_func": lambda: [{"id": 1, "bankid": "", "acctid": ""}],
            "list_fitids_func": lambda account_id: [],
            "list_envelopes_func": lambda: [],
            "import_prefills_func": lambda transactions, account_id, existing_fitids: [],
            "payee_prefills_func": lambda transactions, account_id: [],
            "selected_account_id": 1,
        }

        csv_context = import_review_context(
            {"_source_type": "csv", "transactions": [{"amount_cents": -100, "fitid": ""}]},
            **base_kwargs,
        )
        qfx_context = import_review_context(
            {"_source_type": "qfx", "transactions": [{"amount_cents": -100, "fitid": ""}]},
            **base_kwargs,
        )

        self.assertFalse(csv_context["show_missing_fitid_badges"])
        self.assertTrue(qfx_context["show_missing_fitid_badges"])


    def test_build_import_row_states_centralizes_duplicate_manual_and_prefill_eligibility(self) -> None:
        prefills = [{"row_index": 3, "prefill": True, "single_envelope_id": 9}]

        states = build_import_row_states(
            [
                {"posted_at": "2026-05-01", "amount_cents": -1234, "payee": "Exact", "fitid": "fit-old"},
                {"posted_at": "2026-05-02", "amount_cents": 2500, "payee": "Fuzzy", "fitid": "fit-new"},
                {"posted_at": "2026-05-03", "amount_cents": -300, "payee": "No Fitid", "fitid": ""},
                {"posted_at": "2026-05-04", "amount_cents": -400, "payee": "Prefilled", "fitid": "fit-prefill"},
            ],
            {"fit-old"},
            {1},
            prefills,
        )

        self.assertEqual([state["section"] for state in states], ["exp", "inc", "exp", "exp"])
        self.assertTrue(states[0]["already_imported"])
        self.assertTrue(states[0]["exact_fitid_duplicate"])
        self.assertFalse(states[0]["manual_match_eligible"])
        self.assertFalse(states[0]["prefill_eligible"])
        self.assertTrue(states[1]["already_imported"])
        self.assertTrue(states[1]["fuzzy_transfer_duplicate"])
        self.assertFalse(states[1]["checked"])
        self.assertTrue(states[2]["no_fitid"])
        self.assertTrue(states[2]["manual_match_eligible"])
        self.assertEqual(states[3]["prefill"], prefills[0])

    def test_import_prefills_for_import_review_uses_row_state_for_fuzzy_duplicates(self) -> None:
        transactions = [
            {"fitid": "new-fit", "amount_cents": -100},
            {"fitid": "payment-fit", "amount_cents": 200},
            {"fitid": "other-fit", "amount_cents": -300},
        ]
        row_states = build_import_row_states(transactions, set(), {1})
        calls = []

        prefills = import_prefills_for_import_review(
            transactions,
            42,
            set(),
            row_states=row_states,
            prefill_func=lambda rows, account_id: calls.append((rows, account_id)) or [
                {"row_index": 0, "prefill": True},
                {"row_index": 1, "prefill": True},
            ],
        )

        self.assertEqual(calls, [([transactions[0], transactions[2]], 42)])
        self.assertFalse(prefills[1]["prefill"])
        self.assertEqual(prefills[1]["row_index"], 1)
        self.assertEqual(prefills[1]["debug_reason_codes"], ["already_imported"])
        self.assertEqual(prefills[1]["prediction_debug"]["engine"], "import_prefill")
        self.assertEqual(prefills[1]["prediction_debug"]["decision"], "no_prefill")

    def test_synthetic_payment_rows_have_row_state_contract(self) -> None:
        transactions = [{} for _ in range(58)]
        transactions[8] = {
            "posted_at": "2024-02-17",
            "amount_cents": 84999,
            "payee": "Payment Thank You - Web",
            "fitid": "SYNTHETIC-PAYMENT-ROW-8",
        }
        transactions[57] = {
            "posted_at": "2024-01-22",
            "amount_cents": 181167,
            "payee": "Payment Thank You - Web",
            "fitid": "SYNTHETIC-PAYMENT-ROW-57",
        }

        states = build_import_row_states(transactions, set(), {8})

        self.assertTrue(states[8]["fuzzy_transfer_duplicate"])
        self.assertTrue(states[8]["already_imported"])
        self.assertFalse(states[8]["manual_match_eligible"])
        self.assertEqual(states[57]["amount_cents"], 181167)
        self.assertEqual(states[57]["section"], "inc")
        self.assertTrue(states[57]["manual_match_eligible"])
        self.assertTrue(states[57]["prefill_eligible"])

    def test_import_prefills_for_import_review_skips_existing_fitids_before_prediction(self) -> None:
        transactions = [
            {"fitid": "new-fit", "amount_cents": -100},
            {"fitid": "old-fit", "amount_cents": -200},
            {"fitid": "", "amount_cents": -300},
        ]
        calls = []

        def prefill_func(rows, account_id):
            calls.append((rows, account_id))
            return [
                {"row_index": 0, "prefill": True, "single_envelope_id": 7},
                {"row_index": 1, "prefill": True, "single_envelope_id": 9},
            ]

        prefills = import_prefills_for_import_review(
            transactions,
            42,
            {"old-fit"},
            prefill_func=prefill_func,
        )

        self.assertEqual(calls, [([transactions[0], transactions[2]], 42)])
        self.assertEqual(prefills[0], {"row_index": 0, "prefill": True, "single_envelope_id": 7})
        self.assertFalse(prefills[1]["prefill"])
        self.assertEqual(prefills[1]["row_index"], 1)
        self.assertEqual(prefills[1]["debug_reason_codes"], ["already_imported"])
        self.assertEqual(prefills[1]["prediction_debug"]["engine"], "import_prefill")
        self.assertEqual(prefills[1]["prediction_debug"]["decision"], "no_prefill")
        self.assertEqual(prefills[2], {"row_index": 2, "prefill": True, "single_envelope_id": 9})

    def test_import_prefills_for_import_review_skips_without_account(self) -> None:
        prefills = import_prefills_for_import_review(
            [{"fitid": "new-fit", "amount_cents": -100}],
            None,
            set(),
            prefill_func=lambda rows, account_id: self.fail("should not build prefills without an account"),
        )

        self.assertEqual(prefills, [])

    def test_import_transaction_amount_cents_prefers_parsed_cents_then_amount_strings(self) -> None:
        self.assertEqual(import_transaction_amount_cents({"amount_cents": "-123"}), -123)
        self.assertEqual(import_transaction_amount_cents({"amount": "$12.34"}), 1234)
        self.assertEqual(import_transaction_amount_cents({"trnamt": "-5.67"}), -567)
        self.assertEqual(import_transaction_amount_cents({"amount_cents": "bad"}), 0)

    def test_manual_candidate_date_from_defaults_and_uses_requested_days(self) -> None:
        now = datetime(2026, 5, 1, 12, 0, 0)

        self.assertEqual(manual_candidate_date_from(30, now=now), "2026-04-01")
        self.assertEqual(manual_candidate_date_from(None, now=now), "2016-05-03")

    def test_imported_fitid_details_skips_blanks_and_lets_last_duplicate_win(self) -> None:
        fitids, details = imported_fitid_details(
            [
                {"fitid": " fit-1 ", "payee": "Coffee", "memo": None},
                {"fitid": "", "payee": "Blank", "memo": "ignored"},
                {"fitid": "fit-1", "payee": "Coffee Updated", "memo": "Latte"},
                {"fitid": "fit-2", "payee": None, "memo": None},
            ]
        )

        self.assertEqual(fitids, ["fit-1", "fit-1", "fit-2"])
        self.assertEqual(details["fit-1"], {"payee": "Coffee Updated", "memo": "Latte"})
        self.assertEqual(details["fit-2"], {"payee": "", "memo": ""})

    def test_imported_fitids_request_response_parses_account_id(self) -> None:
        calls = []

        response = imported_fitids_request_response(
            MultiDict({"account_id": "7"}),
            list_imported_fitid_rows_func=lambda account_id: calls.append(account_id) or [{"fitid": "fit-1", "payee": "Coffee", "memo": None}],
        )

        self.assertEqual(response, {"fitids": ["fit-1"], "details": {"fit-1": {"payee": "Coffee", "memo": ""}}, "row_indexes": []})
        self.assertEqual(calls, [7])
        self.assertEqual(
            imported_fitids_request_response(MultiDict(), list_imported_fitid_rows_func=lambda account_id: self.fail("should not load rows")),
            {"fitids": [], "details": {}, "row_indexes": []},
        )

    def test_imported_fitids_response_loads_rows_for_account(self) -> None:
        calls = []

        response = imported_fitids_response(
            42,
            list_imported_fitid_rows_func=lambda account_id: calls.append(account_id) or [
                {"fitid": "fit-1", "payee": "Coffee", "memo": None},
            ],
        )

        self.assertEqual(calls, [42])
        self.assertEqual(response, {
            "fitids": ["fit-1"],
            "details": {"fit-1": {"payee": "Coffee", "memo": ""}},
            "row_indexes": [],
        })
        self.assertEqual(
            imported_fitids_response(None, list_imported_fitid_rows_func=lambda account_id: self.fail("should not load rows")),
            {"fitids": [], "details": {}, "row_indexes": []},
        )

    def test_manual_import_candidate_items_filters_fitids_allocations_and_same_account_transfers(self) -> None:
        paired = {
            20: {"account_id": 200},
            21: {"account_id": 100},
        }

        def get_transaction(tx_id):
            return paired.get(tx_id)

        items = manual_import_candidate_items(
            [
                {"id": 1, "posted_at": "2026-05-01", "amount_cents": "-100", "payee": "Manual", "memo": None, "ttype": "expense"},
                {"id": 2, "posted_at": "2026-05-01", "amount_cents": "-100", "fitid": "fit-1", "ttype": "expense"},
                {"id": 3, "posted_at": "2026-05-01", "amount_cents": "-100", "ttype": "allocation"},
                {"id": 4, "posted_at": "2026-05-01", "amount_cents": "100", "ttype": "transfer_in", "xfer_pair_id": 20},
                {"id": 5, "posted_at": "2026-05-01", "amount_cents": "100", "ttype": "transfer_in", "xfer_pair_id": 21},
                {"id": 6, "posted_at": "2026-05-01", "amount_cents": "100", "ttype": "transfer_in"},
                {"id": 7, "posted_at": "2026-05-01", "amount_cents": "-100", "ttype": "expense", "ignore_match": 1},
                {"id": 8, "posted_at": "2026-05-01", "amount_cents": "100", "fitid": "copied-fit", "ttype": "transfer_in", "xfer_pair_id": 20},
            ],
            account_id=100,
            get_transaction_func=get_transaction,
        )

        self.assertEqual([item["id"] for item in items], [1, 2, 4, 6, 8])
        self.assertEqual(items[0]["amount_cents"], -100)

    def test_manual_import_candidate_items_filters_provenance_matched_transactions(self) -> None:
        items = manual_import_candidate_items(
            [
                {"id": 1, "posted_at": "2026-05-01", "amount_cents": "-100", "payee": "Still open", "memo": None, "ttype": "expense"},
                {"id": 2, "posted_at": "2026-05-02", "amount_cents": "-200", "payee": "Already matched", "memo": None, "ttype": "expense"},
            ],
            account_id=100,
            get_transaction_func=lambda tx_id: None,
            excluded_transaction_ids={2},
        )

        self.assertEqual([item["id"] for item in items], [1])

    def test_manual_import_candidates_request_response_filters_provenance_matched_transactions(self) -> None:
        response = manual_import_candidates_request_response(
            MultiDict({"account_id": "100"}),
            list_transactions_func=lambda **kwargs: ([
                {"id": 1, "posted_at": "2026-05-01", "amount_cents": "-100", "payee": "Still open", "memo": None, "ttype": "expense"},
                {"id": 2, "posted_at": "2026-05-02", "amount_cents": "-200", "payee": "Already matched", "memo": None, "ttype": "expense"},
            ], 2),
            get_transaction_func=lambda tx_id: None,
            list_import_matched_transaction_ids_func=lambda account_id: {2},
        )

        self.assertEqual([item["id"] for item in response["items"]], [1])

    def test_manual_import_candidates_excludes_current_statement_fitid_transfer(self) -> None:
        paired = {20: {"account_id": 200}}

        response = manual_import_candidates_response(
            100,
            30,
            list_transactions_func=lambda **kwargs: ([
                {
                    "id": 8,
                    "posted_at": "2026-05-01",
                    "amount_cents": "-1000",
                    "payee": "Card Payment",
                    "memo": None,
                    "fitid": "directional-fit-100",
                    "ttype": "transfer_out",
                    "xfer_pair_id": 20,
                },
            ], 1),
            get_transaction_func=lambda tx_id: paired.get(tx_id),
            import_rows=[{
                "index": 0,
                "posted_at": "2026-05-01",
                "amount_cents": -1000,
                "payee": "Card Payment",
                "fitid": "directional-fit-100",
            }],
        )

        self.assertEqual(response["items"], [])

    def test_manual_import_candidates_preserves_legacy_copied_fitid_counterparty(self) -> None:
        paired = {20: {"account_id": 200}}

        response = manual_import_candidates_response(
            100,
            30,
            list_transactions_func=lambda **kwargs: ([
                {
                    "id": 8,
                    "posted_at": "2026-05-01",
                    "amount_cents": "-1000",
                    "payee": "Example Bank Payment",
                    "memo": None,
                    "fitid": "legacy-copied-other-side-fitid",
                    "ttype": "transfer_out",
                    "xfer_pair_id": 20,
                },
            ], 1),
            get_transaction_func=lambda tx_id: paired.get(tx_id),
            import_rows=[{
                "index": 0,
                "posted_at": "2026-05-01",
                "amount_cents": -1000,
                "payee": "Example Bank Payment",
                "fitid": "directional-fit-100",
            }],
        )

        self.assertEqual([item["id"] for item in response["items"]], [8])
        self.assertEqual(response["items"][0]["suggested_import_index"], 0)

    def test_manual_import_candidates_response_filters_prior_account_fitid_transfer(self) -> None:
        paired = {20: {"account_id": 200}}

        response = manual_import_candidates_response(
            100,
            30,
            list_transactions_func=lambda **kwargs: ([
                {
                    "id": 8,
                    "posted_at": "2023-04-01",
                    "amount_cents": "-1000",
                    "payee": "Old Example Bank Payment",
                    "memo": None,
                    "fitid": "previously-matched-0303-fitid",
                    "ttype": "transfer_out",
                    "xfer_pair_id": 20,
                },
            ], 1),
            get_transaction_func=lambda tx_id: paired.get(tx_id),
            import_rows=[{
                "index": 0,
                "posted_at": "2026-05-01",
                "amount_cents": -1000,
                "payee": "New Example Bank Payment",
                "fitid": "new-directional-fitid",
            }],
            existing_fitids={"previously-matched-0303-fitid"},
        )

        self.assertEqual(response["items"], [])

    def test_manual_import_candidates_response_filters_prior_example_bank_payment_income_fitid(self) -> None:
        paired = {184: {"account_id": 1}}

        response = manual_import_candidates_response(
            8,
            3650,
            list_transactions_func=lambda **kwargs: ([
                {
                    "id": 185,
                    "posted_at": "2026-03-27",
                    "amount_cents": "12800",
                    "payee": "Payment Thank You - Web",
                    "memo": None,
                    "fitid": "DEMO-PRIOR-INCOME-FITID",
                    "ttype": "transfer_in",
                    "xfer_pair_id": 184,
                },
            ], 1),
            get_transaction_func=lambda tx_id: paired.get(tx_id),
            import_rows=[{
                "index": 156,
                "posted_at": "2026-03-27",
                "amount_cents": 12800,
                "payee": "Payment Thank You - Web",
                "fitid": "DEMO-PRIOR-INCOME-FITID",
            }],
            existing_fitids={"DEMO-PRIOR-INCOME-FITID"},
        )

        self.assertEqual(response["items"], [])

    def test_manual_import_candidates_request_response_filters_existing_fitids_when_import_payload_empty(self) -> None:
        response = manual_import_candidates_request_response(
            MultiDict({"account_id": "100", "imports": "[]"}),
            list_transactions_func=lambda **kwargs: ([
                {
                    "id": 8,
                    "posted_at": "2026-05-01",
                    "amount_cents": "1000",
                    "payee": "Already imported payment",
                    "memo": None,
                    "fitid": "already-imported-fitid",
                    "ttype": "transfer_in",
                    "xfer_pair_id": 20,
                },
            ], 1),
            get_transaction_func=lambda tx_id: {"account_id": 200},
            list_imported_fitid_rows_func=lambda account_id: [
                {"fitid": "already-imported-fitid", "payee": "Already imported payment", "memo": None},
            ],
        )

        self.assertEqual(response["items"], [])

    def test_manual_import_candidates_request_response_filters_current_fitid_transfer(self) -> None:
        response = manual_import_candidates_request_response(
            MultiDict({
                "account_id": "100",
                "imports": '[{"index": 0, "posted_at": "2026-05-01", "amount_cents": -1000, "payee": "Card Payment", "fitid": "directional-fit-100"}]',
            }),
            list_transactions_func=lambda **kwargs: ([
                {
                    "id": 8,
                    "posted_at": "2026-05-01",
                    "amount_cents": "-1000",
                    "payee": "Card Payment",
                    "memo": None,
                    "fitid": "directional-fit-100",
                    "ttype": "transfer_out",
                    "xfer_pair_id": 20,
                },
            ], 1),
            get_transaction_func=lambda tx_id: {"account_id": 200},
            list_imported_fitid_rows_func=lambda account_id: [
                {"fitid": "directional-fit-100", "payee": "Card Payment", "memo": None},
            ],
        )

        self.assertEqual(response["items"], [])

    def test_manual_import_candidates_request_response_parses_args(self) -> None:
        calls = []

        response = manual_import_candidates_request_response(
            MultiDict({"account_id": "42", "days": "30"}),
            list_transactions_func=lambda **kwargs: calls.append(kwargs) or ([], None),
            get_transaction_func=lambda transaction_id: None,
        )

        self.assertEqual(response, {"items": [], "overflow_items": []})
        self.assertEqual(calls, [{"limit": 1000, "account_id": 42, "date_from": manual_candidate_date_from(30)}])
        self.assertEqual(
            manual_import_candidates_request_response(
                MultiDict(),
                list_transactions_func=lambda **kwargs: self.fail("should not load transactions without account_id"),
                get_transaction_func=lambda transaction_id: None,
            ),
            {"items": [], "overflow_items": []},
        )

    def test_manual_import_candidates_response_loads_recent_rows_for_account(self) -> None:
        calls = []

        def list_transactions(**kwargs):
            calls.append(kwargs)
            return ([
                {"id": 1, "posted_at": "2026-05-01", "amount_cents": "-100", "payee": "Manual", "memo": None, "ttype": "expense"},
            ], 1)

        response = manual_import_candidates_response(
            100,
            30,
            list_transactions_func=list_transactions,
            get_transaction_func=lambda tx_id: None,
        )

        self.assertEqual(response["items"][0]["id"], 1)
        self.assertEqual(calls, [{"limit": 1000, "account_id": 100, "date_from": calls[0]["date_from"]}])
        self.assertRegex(calls[0]["date_from"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(
            manual_import_candidates_response(None, 30, list_transactions_func=list_transactions, get_transaction_func=lambda tx_id: None),
            {"items": [], "overflow_items": []},
        )

    def test_parse_statement_upload_adds_file_hash_and_parses_by_filename_suffix(self) -> None:
        data = (
            b"<OFX>\n"
            b"<BANKID>EXAMPLEBANK\n"
            b"<ACCTID>DEMOACCT0202\n"
            b"<STMTTRN>\n"
            b"<DTPOSTED>20260429120000[-8:UTC]\n"
            b"<TRNAMT>-45.67\n"
            b"<NAME>Grocery Store\n"
            b"<FITID>DEMO-FIT-001\n"
            b"</STMTTRN>\n"
        )

        parsed = parse_statement_upload(data, "statement.qfx")

        self.assertEqual(parsed["file_hash"], hashlib.sha256(data).hexdigest())
        self.assertEqual(parsed["bankid"], "EXAMPLEBANK")
        self.assertEqual(parsed["transactions"][0]["amount_cents"], -4567)

    def test_parse_csv_reads_common_columns(self) -> None:
        with TemporaryDirectory(prefix="finance-import-csv-") as td:
            path = Path(td) / "statement.csv"
            path.write_text(
                "Date,Amount,Name,Memo,Id\n"
                "2026-04-28,-12.34,Coffee Shop,Morning latte,abc123\n",
                encoding="utf-8",
            )

            parsed = parse_csv(path)

        self.assertIsNone(parsed["bankid"])
        self.assertIsNone(parsed["acctid"])
        self.assertEqual(len(parsed["transactions"]), 1)
        tx = parsed["transactions"][0]
        self.assertEqual(tx["posted_at"], "2026-04-28")
        self.assertEqual(tx["amount_cents"], -1234)
        self.assertEqual(tx["payee"], "Coffee Shop")
        self.assertEqual(tx["memo"], "Morning latte")
        self.assertEqual(tx["fitid"], "abc123")

    def test_parse_csv_detects_transaction_date_description_type_headers(self) -> None:
        with TemporaryDirectory(prefix="finance-import-csv-") as td:
            path = Path(td) / "example-investing.csv"
            path.write_text(
                "Transaction date,Description,Type,Amount\n"
                "01/15/2026,Example Bank (Account ****0202),Deposit,12.50\n",
                encoding="utf-8",
            )

            parsed = parse_csv(path)

        tx = parsed["transactions"][0]
        self.assertEqual(tx["posted_at"], "2026-01-15")
        self.assertEqual(tx["amount_cents"], 1250)
        self.assertEqual(tx["payee"], "Example Bank (Account ****0202)")
        self.assertEqual(tx["memo"], "Deposit")

    def test_parse_csv_detects_reference_and_address_headers(self) -> None:
        with TemporaryDirectory(prefix="finance-import-csv-") as td:
            path = Path(td) / "example-bank.csv"
            path.write_text(
                "Posted Date,Reference Number,Payee,Address,Amount\n"
                "04/28/2026,ref-001,Coffee Shop,Main St,-12.34\n",
                encoding="utf-8",
            )

            parsed = parse_csv(path)

        tx = parsed["transactions"][0]
        self.assertEqual(tx["posted_at"], "2026-04-28")
        self.assertEqual(tx["amount_cents"], -1234)
        self.assertEqual(tx["payee"], "Coffee Shop")
        self.assertEqual(tx["memo"], "Main St")
        self.assertEqual(tx["fitid"], "ref-001")

    def test_credit_card_csv_polarity_detection_flags_positive_charges_and_negative_payments(self) -> None:
        with TemporaryDirectory(prefix="finance-import-csv-") as td:
            path = Path(td) / "credit-card.csv"
            path.write_text(
                "Reference Number,Transaction Post Date,Description of Transaction,Transaction Type,Amount\n"
                "1,06/25/26,Demo Grocery,clearing,50.00\n"
                "2,06/24/26,Demo Cafe,clearing,20.00\n"
                "3,06/23/26,Demo Pharmacy,clearing,30.00\n"
                "4,06/22/26,,payment_transaction,-100.00\n",
                encoding="utf-8",
            )

            parsed = parse_csv(path)
            parsed["_source_type"] = "csv"

        self.assertEqual(
            detect_csv_credit_card_polarity(parsed, {"account_type": "credit_card"}),
            "inverted",
        )
        apply_csv_polarity(parsed, "inverted")
        self.assertEqual([row["amount_cents"] for row in parsed["transactions"]], [-5000, -2000, -3000, 10000])
        states = build_import_row_states(parsed["transactions"], set())
        self.assertEqual([state["section"] for state in states], ["exp", "exp", "exp", "inc"])

    def test_parse_csv_requires_mapping_for_missing_date_instead_of_today_fallback(self) -> None:
        with TemporaryDirectory(prefix="finance-import-csv-") as td:
            path = Path(td) / "statement.csv"
            path.write_text("When,Amount,Name\nnot-a-date,-12.34,Coffee\n", encoding="utf-8")

            with self.assertRaises(CsvColumnMappingRequired):
                parse_csv(path)

    def test_parse_csv_applies_user_mapping_and_debit_credit_pair(self) -> None:
        with TemporaryDirectory(prefix="finance-import-csv-") as td:
            path = Path(td) / "statement.csv"
            path.write_text(
                "When,Who,Debit,Credit\n"
                "2026-04-28,Coffee,12.34,\n"
                "2026-04-29,Paycheck,,100.00\n",
                encoding="utf-8",
            )

            parsed = parse_csv(path, mapping={"date": "When", "payee": "Who", "debit": "Debit", "credit": "Credit"})

        self.assertEqual([tx["amount_cents"] for tx in parsed["transactions"]], [-1234, 10000])
        self.assertEqual([tx["posted_at"] for tx in parsed["transactions"]], ["2026-04-28", "2026-04-29"])

    def test_parse_qfx_extracts_basic_transaction_fields(self) -> None:
        with TemporaryDirectory(prefix="finance-import-qfx-") as td:
            path = Path(td) / "statement.qfx"
            path.write_text(
                "<OFX>\n"
                "<BANKID>EXAMPLEBANK\n"
                "<ACCTID>DEMOACCT0202\n"
                "<STMTTRN>\n"
                "<DTPOSTED>20260429120000[-8:UTC]\n"
                "<TRNAMT>-45.67\n"
                "<NAME>Grocery Store\n"
                "<MEMO>Weekly groceries\n"
                "<FITID>DEMO-FIT-001\n"
                "</STMTTRN>\n",
                encoding="utf-8",
            )

            parsed = parse_qfx(path)

        self.assertEqual(parsed["bankid"], "EXAMPLEBANK")
        self.assertEqual(parsed["acctid"], "DEMOACCT0202")
        self.assertEqual(len(parsed["transactions"]), 1)
        tx = parsed["transactions"][0]
        self.assertEqual(tx["posted_at"], "2026-04-29")
        self.assertEqual(tx["amount_cents"], -4567)
        self.assertEqual(tx["payee"], "Grocery Store - Weekly groceries")
        self.assertIsNone(tx["memo"])
        self.assertEqual(tx["fitid"], "DEMO-FIT-001")

    def test_combine_qfx_payee_and_memo_avoids_duplicate_text(self) -> None:
        self.assertEqual(combine_qfx_payee_and_memo("Store", "Store"), ("Store", None))
        self.assertEqual(combine_qfx_payee_and_memo("Store", "Store #123"), ("Store #123", None))
        self.assertEqual(combine_qfx_payee_and_memo("Store #123", "Store"), ("Store #123", None))

    def test_combine_qfx_payee_and_memo_uses_separator_for_distinct_text(self) -> None:
        self.assertEqual(
            combine_qfx_payee_and_memo("Grocery Store", "Weekly groceries"),
            ("Grocery Store - Weekly groceries", None),
        )
