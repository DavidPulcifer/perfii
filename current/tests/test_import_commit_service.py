import json

from werkzeug.datastructures import MultiDict

from app.services.import_commit_service import (
    ImportSourceUnavailableError,
    ImportCommitTally,
    ImportSplitPlan,
    StatementIdentifiers,
    bind_statement_identifiers_if_missing,
    build_import_commit_plan,
    build_prediction_feedback_rows_for_commit,
    collect_import_creation_split_plan,
    collect_import_creation_splits,
    collect_import_row_split_plan,
    collect_import_row_splits,
    collect_import_transfer_from_split_plan,
    collect_import_transfer_from_splits,
    collect_import_transfer_split_plan,
    collect_import_transfer_splits,
    commit_import_row,
    commit_import_rows,
    create_import_standard_transaction_from_row,
    create_import_transaction_from_row,
    create_import_transfer_from_row,
    determine_import_transaction_type,
    finalize_import_commit,
    flash_invalid_match_skip,
    flash_missing_transfer_account_skip,
    ignored_transaction_ids,
    ignored_transaction_payload,
    import_result_flash,
    provenance_record_for_row,
    import_row_count,
    import_row_has_conflicting_split_and_transfer_config,
    import_row_split_plan_invalid,
    import_transfer_is_out,
    import_row_is_duplicate,
    import_transfer_splits,
    is_import_transfer_transaction_type,
    invalid_match_flash_message,
    invalid_match_skip_reason,
    load_import_commit_account,
    mark_ignored_transactions,
    match_target_belongs_to_account,
    matched_transaction_amount_cents,
    matched_transaction_payload,
    remember_imported_fitid,
    missing_transfer_account_flash_message,
    missing_transfer_account_skip_reason,
    parse_import_row_form,
    perform_import_commit,
    prepare_import_commit_context,
    selected_import_row_indices,
    statement_identifiers_from_source,
    should_bind_statement_identifiers,
    standard_transaction_payload,
    transfer_transaction_payload,
    unexpected_import_error_skip_reason,
    update_import_matched_transaction,
)
from tests.helpers import FinanceAppTestCase


class ImportCommitServiceTests(FinanceAppTestCase):
    def test_load_import_commit_account_validates_and_loads_account(self) -> None:
        loaded_ids = []

        missing_account = load_import_commit_account(MultiDict(), lambda account_id: loaded_ids.append(account_id))
        not_found = load_import_commit_account(
            MultiDict({"account_id": "42"}),
            lambda account_id: loaded_ids.append(account_id) or None,
        )
        found = load_import_commit_account(
            MultiDict({"account_id": "7"}),
            lambda account_id: loaded_ids.append(account_id) or {"id": account_id, "name": "Checking"},
        )

        self.assertFalse(missing_account.ok)
        self.assertEqual(missing_account.error_message, "Pick an account to import into.")
        self.assertEqual(missing_account.flash_category, "warning")
        self.assertFalse(not_found.ok)
        self.assertEqual(not_found.account_id, 42)
        self.assertEqual(not_found.error_message, "Account not found.")
        self.assertEqual(not_found.flash_category, "danger")
        self.assertTrue(found.ok)
        self.assertEqual(found.account_id, 7)
        self.assertEqual(found.account, {"id": 7, "name": "Checking"})
        self.assertEqual(loaded_ids, [42, 7])

    def test_statement_identifiers_from_source_normalizes_blanks(self) -> None:
        identifiers = statement_identifiers_from_source({
            "source_bankid": "  BANK123  ",
            "source_acctid": "   ",
        })

        self.assertEqual(identifiers.bankid, "BANK123")
        self.assertIsNone(identifiers.acctid)
        self.assertTrue(identifiers.has_any)

    def test_bind_statement_identifiers_updates_unidentified_account_and_local_copy(self) -> None:
        identifiers = StatementIdentifiers(bankid="BANK123", acctid="ACCT456")
        account = {"bankid": "", "acctid": None}
        calls = []

        did_bind = bind_statement_identifiers_if_missing(
            7,
            account,
            identifiers,
            lambda account_id, payload: calls.append((account_id, payload)),
        )

        self.assertTrue(did_bind)
        self.assertEqual(calls, [(7, {"bankid": "BANK123", "acctid": "ACCT456"})])
        self.assertEqual(account["bankid"], "BANK123")
        self.assertEqual(account["acctid"], "ACCT456")

    def test_bind_statement_identifiers_skips_identified_or_missing_accounts(self) -> None:
        identifiers = StatementIdentifiers(bankid="BANK123", acctid=None)
        calls = []

        self.assertFalse(bind_statement_identifiers_if_missing(7, {"bankid": "EXISTING", "acctid": ""}, identifiers, lambda *args: calls.append(args)))
        self.assertFalse(bind_statement_identifiers_if_missing(7, None, identifiers, lambda *args: calls.append(args)))
        self.assertEqual(calls, [])

    def test_should_bind_statement_identifiers_only_for_unidentified_accounts(self) -> None:
        identifiers = StatementIdentifiers(bankid="BANK123", acctid="ACCT456")

        self.assertTrue(should_bind_statement_identifiers({"bankid": "", "acctid": None}, identifiers))
        self.assertFalse(should_bind_statement_identifiers({"bankid": "EXISTING", "acctid": ""}, identifiers))
        self.assertFalse(should_bind_statement_identifiers({"bankid": "", "acctid": "EXISTING"}, identifiers))
        self.assertFalse(should_bind_statement_identifiers(None, identifiers))
        self.assertFalse(should_bind_statement_identifiers({"bankid": "", "acctid": ""}, StatementIdentifiers(bankid=None, acctid=None)))

    def test_perform_import_commit_runs_setup_rows_and_finalization(self) -> None:
        calls = []
        flashes = []
        form = MultiDict({"account_id": "7", "count": "0"})

        tally, matched_ids = perform_import_commit(
            account_id=7,
            account={"id": 7, "bankid": "BANK", "acctid": "ACCT"},
            form=form,
            list_fitids_func=lambda account_id: calls.append(("fitids", account_id)) or {"seen-fitid"},
            update_account_func=lambda *args: calls.append(("update_account", args)),
            get_transaction_func=lambda tx_id: calls.append(("get_tx", tx_id)) or None,
            edit_transaction_func=lambda **kwargs: calls.append(("edit", kwargs)),
            get_account_func=lambda account_id: calls.append(("get_account", account_id)) or None,
            create_transfer_func=lambda **kwargs: calls.append(("transfer", kwargs)),
            create_expense_func=lambda **kwargs: calls.append(("expense", kwargs)),
            create_income_func=lambda **kwargs: calls.append(("income", kwargs)),
            logger=None,
            flash_func=lambda message, category: flashes.append((message, category)),
        )

        self.assertEqual(tally.imported, 0)
        self.assertEqual(tally.skipped, 0)
        self.assertEqual(matched_ids, set())
        self.assertEqual(flashes, [("Imported 0 transaction(s). Skipped 0.", "success")])
        self.assertEqual(calls, [("fitids", 7)])

    def test_prepare_import_commit_context_loads_fitids_and_identifier_binding(self) -> None:
        calls = []
        account = {"bankid": "", "acctid": ""}
        form = MultiDict({
            "count": "3",
            "import_source_token": "source-token",
        })

        context = prepare_import_commit_context(
            account_id=7,
            account=account,
            form=form,
            list_fitids_func=lambda account_id: calls.append(("fitids", account_id)) or ["fit-1"],
            update_account_func=lambda account_id, payload: calls.append(("update", account_id, payload)),
            get_import_review_source_func=lambda token, account_id: calls.append(("source", token, account_id)) or {
                "source_bankid": "BANK123",
                "source_acctid": "ACCT456",
                "file_hash": "hash-1",
            },
        )

        self.assertEqual(context.count, 3)
        self.assertEqual(context.existing_fitids, {"fit-1"})
        self.assertEqual(context.source_bankid, "BANK123")
        self.assertEqual(context.source_acctid, "ACCT456")
        self.assertEqual(context.file_hash, "hash-1")
        self.assertEqual(account, {"bankid": "BANK123", "acctid": "ACCT456"})
        self.assertEqual(calls, [
            ("fitids", 7),
            ("source", "source-token", 7),
            ("update", 7, {"bankid": "BANK123", "acctid": "ACCT456"}),
        ])

    def test_prepare_import_commit_context_fails_closed_for_invalid_source_token(self) -> None:
        form = MultiDict({"count": "1", "import_source_token": "missing"})

        with self.assertRaisesRegex(ImportSourceUnavailableError, "Upload the statement again"):
            prepare_import_commit_context(
                account_id=7,
                account={"bankid": "", "acctid": ""},
                form=form,
                list_fitids_func=lambda account_id: [],
                update_account_func=lambda account_id, payload: None,
                get_import_review_source_func=lambda token, account_id: None,
            )


    def test_build_import_commit_plan_classifies_create_match_transfer_and_skip(self) -> None:
        form = MultiDict({
            "count": "4",
            "row_0": "on", "amount_0": "-10.00", "fitid_0": "new-exp",
            "row_1": "on", "amount_1": "25.00", "fitid_1": "seen-fit",
            "row_2": "on", "amount_2": "-5.00", "match_tx_2": "77",
            "row_3": "on", "amount_3": "-40.00", "is_transfer_3": "on", "transfer_account_3": "9",
        })

        plan = build_import_commit_plan(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            count=4,
            existing_fitids={"seen-fit"},
            get_transaction_func=lambda tx_id: {"id": tx_id, "account_id": 7, "amount_cents": -500},
            get_account_func=lambda account_id: {"id": account_id, "account_type": "bank"},
        )

        self.assertEqual([item.action for item in plan.items], ["create", "skip", "match", "transfer"])

    def test_build_import_commit_plan_revalidates_invalid_match_and_transfer_before_writes(self) -> None:
        form = MultiDict({
            "count": "2",
            "row_0": "on", "amount_0": "-5.00", "match_tx_0": "77",
            "row_1": "on", "amount_1": "-40.00", "is_transfer_1": "on",
        })

        plan = build_import_commit_plan(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            count=2,
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: {"id": tx_id, "account_id": 8, "amount_cents": -500},
            get_account_func=lambda account_id: None,
        )

        self.assertEqual([item.action for item in plan.items], ["skip", "skip"])
        self.assertEqual(plan.items[0].skip_reason, "Row 1: match target not found")
        self.assertEqual(plan.items[1].skip_reason, "Row 2: transfer needs another account")

    def test_build_import_commit_plan_rejects_split_and_transfer_on_same_row(self) -> None:
        form = MultiDict({
            "count": "1",
            "row_0": "on",
            "amount_0": "-40.00",
            "is_transfer_0": "on",
            "transfer_account_0": "9",
            "exp_amount_0_3": "40.00",
            "trf_from_amt_0_4": "40.00",
            "trf_amt_0_5": "40.00",
        })

        plan = build_import_commit_plan(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            count=1,
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: None,
            get_account_func=lambda account_id: {"id": account_id, "account_type": "bank"},
        )

        self.assertEqual([item.action for item in plan.items], ["skip"])
        self.assertEqual(plan.items[0].skip_reason, "Row 1: choose split or transfer, not both")

    def test_import_row_conflict_detects_standard_and_transfer_remainders(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "40.00",
            "posted_at_0": "2026-05-12",
            "inc_remainder_0": "3",
            "trf_remainder_0": "4",
        }), 0)

        self.assertTrue(import_row_has_conflicting_split_and_transfer_config(
            MultiDict({"inc_remainder_0": "3", "trf_remainder_0": "4"}),
            row,
        ))


    def test_commit_import_rows_records_durable_provenance_for_manual_match(self) -> None:
        form = MultiDict({
            "count": "1",
            "row_0": "on",
            "posted_at_0": "2026-05-03",
            "amount_0": "-12.99",
            "payee_0": "Coffee Shop",
            "orig_payee_0": "COFFEE SHOP",
            "memo_0": "Latte",
            "fitid_0": "new-fitid",
            "match_tx_0": "77",
        })
        recorded = []

        tally, matched_ids = commit_import_rows(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            count=1,
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: {"id": tx_id, "account_id": 7, "amount_cents": -1299},
            edit_transaction_func=lambda **kwargs: None,
            get_account_func=lambda account_id: None,
            create_transfer_func=lambda **kwargs: None,
            create_expense_func=lambda **kwargs: None,
            create_income_func=lambda **kwargs: None,
            logger=None,
            flash_func=lambda *args: None,
            source_bankid="BANK",
            source_acctid="ACCT",
            file_hash="hash-1",
            record_import_provenance_func=lambda **kwargs: recorded.append(kwargs),
        )

        self.assertEqual(tally.imported, 1)
        self.assertEqual(matched_ids, {77})
        self.assertEqual(recorded[0]["account_id"], 7)
        self.assertEqual(recorded[0]["source_bankid"], "BANK")
        self.assertEqual(recorded[0]["rows"][0]["match_type"], "manual_match")
        self.assertEqual(recorded[0]["rows"][0]["transaction_ids"], [77])
        self.assertEqual(recorded[0]["rows"][0]["evidence"]["file_hash"], "hash-1")


    def test_commit_import_rows_records_payee_normalization_after_successful_import(self) -> None:
        form = MultiDict({
            "count": "1",
            "row_0": "on",
            "posted_at_0": "2026-05-03",
            "amount_0": "-12.99",
            "payee_0": "Coffee Shop",
            "orig_payee_0": "SQ *COFFEE 1234",
            "memo_0": "Latte",
            "orig_memo_0": "CARD 0001",
            "fitid_0": "fit-1",
            "exp_single_0": "9",
        })
        learned = []

        tally, _matched_ids = commit_import_rows(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            count=1,
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: None,
            edit_transaction_func=lambda **kwargs: None,
            get_account_func=lambda account_id: None,
            create_transfer_func=lambda **kwargs: None,
            create_expense_func=lambda **kwargs: 101,
            create_income_func=lambda **kwargs: None,
            logger=None,
            flash_func=lambda *args: None,
            record_import_provenance_func=None,
            record_payee_normalization_func=lambda row, account_id: learned.append((account_id, row.orig_payee, row.orig_memo, row.payee)),
        )

        self.assertEqual(tally.imported, 1)
        self.assertEqual(learned, [(7, "SQ *COFFEE 1234", "CARD 0001", "Coffee Shop")])

    def test_prediction_feedback_records_accepted_unchanged_prediction(self) -> None:
        predicted = {
            "prediction_type": "new_transaction",
            "transaction_type": "expense",
            "single_envelope_id": 9,
            "splits": [],
            "remainder_envelope_id": None,
            "remainder_amount_cents": None,
            "transfer": None,
        }
        form = MultiDict({
            "count": "1",
            "row_0": "on",
            "amount_0": "-12.00",
            "payee_0": "Coffee",
            "exp_single_0": "9",
            "prediction_feedback_json": json.dumps({
                "items": [{
                    "row_index": 0,
                    "prediction_id": "pred-accepted",
                    "learning_example_id": 44,
                    "prediction_type": "new_transaction",
                    "predicted_json": predicted,
                    "status": "applied",
                }],
            }),
        })
        plan = build_import_commit_plan(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            count=1,
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: None,
            get_account_func=lambda account_id: None,
        )

        rows = build_prediction_feedback_rows_for_commit(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            plan=plan,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "accepted")
        self.assertEqual(rows[0]["accepted"], 1)
        self.assertEqual(rows[0]["modified"], 0)
        self.assertEqual(rows[0]["rejected"], 0)
        self.assertEqual(json.loads(rows[0]["predicted_json"])["single_envelope_id"], 9)
        self.assertEqual(json.loads(rows[0]["final_json"])["single_envelope_id"], 9)

    def test_prediction_feedback_records_modified_prediction(self) -> None:
        predicted = {
            "prediction_type": "new_transaction",
            "transaction_type": "expense",
            "single_envelope_id": 9,
            "splits": [],
            "remainder_envelope_id": None,
            "remainder_amount_cents": None,
            "transfer": None,
        }
        form = MultiDict({
            "count": "1",
            "row_0": "on",
            "amount_0": "-12.00",
            "payee_0": "Coffee",
            "exp_single_0": "10",
            "prediction_feedback_json": json.dumps({
                "items": [{
                    "row_index": 0,
                    "prediction_id": "pred-modified",
                    "prediction_type": "new_transaction",
                    "predicted_json": predicted,
                }],
            }),
        })
        plan = build_import_commit_plan(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            count=1,
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: None,
            get_account_func=lambda account_id: None,
        )

        rows = build_prediction_feedback_rows_for_commit(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            plan=plan,
        )

        self.assertEqual(rows[0]["outcome"], "modified")
        self.assertEqual(rows[0]["accepted"], 0)
        self.assertEqual(rows[0]["modified"], 1)
        self.assertEqual(rows[0]["rejected"], 0)
        self.assertEqual(json.loads(rows[0]["predicted_json"])["single_envelope_id"], 9)
        self.assertEqual(json.loads(rows[0]["final_json"])["single_envelope_id"], 10)

    def test_prediction_feedback_records_cleared_prediction(self) -> None:
        predicted = {
            "prediction_type": "new_transaction",
            "transaction_type": "expense",
            "single_envelope_id": 9,
            "splits": [],
            "remainder_envelope_id": None,
            "remainder_amount_cents": None,
            "transfer": None,
        }
        form = MultiDict({
            "count": "1",
            "row_0": "on",
            "amount_0": "-12.00",
            "payee_0": "Coffee",
            "prediction_feedback_json": json.dumps({
                "items": [{
                    "row_index": 0,
                    "prediction_id": "pred-cleared",
                    "prediction_type": "new_transaction",
                    "predicted_json": predicted,
                    "status": "cleared",
                }],
            }),
        })
        plan = build_import_commit_plan(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            count=1,
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: None,
            get_account_func=lambda account_id: None,
        )

        rows = build_prediction_feedback_rows_for_commit(
            account_id=7,
            account={"id": 7, "account_type": "bank"},
            form=form,
            plan=plan,
        )

        self.assertEqual(rows[0]["outcome"], "cleared")
        self.assertEqual(rows[0]["accepted"], 0)
        self.assertEqual(rows[0]["modified"], 0)
        self.assertEqual(rows[0]["rejected"], 1)
        self.assertIsNone(json.loads(rows[0]["final_json"])["single_envelope_id"])

    def test_import_row_split_plan_invalid_detects_unbalanced_explicit_splits(self) -> None:
        row = parse_import_row_form(MultiDict({"amount_0": "-10.00"}), 0)

        self.assertTrue(import_row_split_plan_invalid(MultiDict({"exp_amount_0_5": "3.00"}), row))
        self.assertFalse(import_row_split_plan_invalid(MultiDict({"exp_amount_0_5": "3.00", "exp_remainder_0": "6"}), row))

    def test_import_commit_tally_records_imports_skips_and_flash(self) -> None:
        tally = ImportCommitTally()

        tally.record_imported()
        tally.record_skipped("bad row")
        tally.record_skipped()

        self.assertEqual(tally.imported, 1)
        self.assertEqual(tally.skipped, 2)
        self.assertEqual(tally.skipped_reasons, ["bad row"])
        self.assertEqual(tally.flash(), ("Imported 1 transaction(s). Skipped 2. (bad row)", "warning"))

    def test_import_commit_tally_uses_independent_reason_lists(self) -> None:
        first = ImportCommitTally()
        second = ImportCommitTally()

        first.record_skipped("first only")

        self.assertEqual(first.skipped_reasons, ["first only"])
        self.assertEqual(second.skipped_reasons, [])

    def test_import_row_count_normalizes_count_field(self) -> None:
        self.assertEqual(import_row_count(MultiDict({"count": "3"})), 3)
        self.assertEqual(import_row_count(MultiDict({"count": ""})), 0)
        self.assertEqual(import_row_count(MultiDict({"count": "not-a-number"})), 0)
        self.assertEqual(import_row_count(MultiDict({"count": "-2"})), 0)

    def test_selected_import_row_indices_returns_checked_rows_only(self) -> None:
        form = MultiDict({"row_0": "on", "row_2": "on", "row_5": "on"})

        self.assertEqual(selected_import_row_indices(form, 4), [0, 2])

    def test_selected_import_row_indices_clamps_negative_count(self) -> None:
        self.assertEqual(selected_import_row_indices(MultiDict({"row_0": "on"}), -1), [])

    def test_parse_import_row_form_normalizes_row_fields(self) -> None:
        form = MultiDict({
            "row_4": "on",
            "posted_at_4": "2026-05-10",
            "amount_4": "-12.34",
            "payee_4": "  Coffee Shop  ",
            "orig_payee_4": " COFFEE SHOP #123 ",
            "memo_4": "  Latte  ",
            "orig_memo_4": "  RAW MEMO  ",
            "fitid_4": " fit-123 ",
            "match_tx_4": "42",
            "match_amt_src_4": " Import ",
            "is_transfer_4": "on",
            "transfer_account_4": "8",
        })

        row = parse_import_row_form(form, 4)

        self.assertTrue(row.selected)
        self.assertEqual(row.posted_at, "2026-05-10")
        self.assertEqual(row.amount_cents, -1234)
        self.assertEqual(row.payee, "Coffee Shop")
        self.assertEqual(row.orig_payee, "COFFEE SHOP #123")
        self.assertEqual(row.memo, "Latte")
        self.assertEqual(row.orig_memo, "RAW MEMO")
        self.assertEqual(row.fitid, "fit-123")
        self.assertEqual(row.match_tx_id, 42)
        self.assertEqual(row.match_amount_source, "import")
        self.assertTrue(row.is_transfer)
        self.assertEqual(row.transfer_account_id, 8)

    def test_parse_import_row_form_rejects_invalid_row_amount(self) -> None:
        form = MultiDict({"row_0": "on", "amount_0": "not-money"})

        with self.assertRaises(ValueError):
            parse_import_row_form(form, 0)

    def test_determine_import_transaction_type_uses_sign_without_transfer_flag(self) -> None:
        expense_row = parse_import_row_form(MultiDict({"amount_0": "-10.00"}), 0)
        income_row = parse_import_row_form(MultiDict({"amount_0": "10.00"}), 0)

        self.assertEqual(determine_import_transaction_type(expense_row, "bank"), "expense")
        self.assertEqual(determine_import_transaction_type(income_row, "bank"), "income")

    def test_determine_import_transaction_type_uses_transfer_direction_from_sign(self) -> None:
        out_row = parse_import_row_form(MultiDict({"amount_0": "-10.00", "is_transfer_0": "on"}), 0)
        in_row = parse_import_row_form(MultiDict({"amount_0": "10.00", "is_transfer_0": "on"}), 0)

        self.assertEqual(determine_import_transaction_type(out_row, "bank"), "transfer_out")
        self.assertEqual(determine_import_transaction_type(in_row, "bank"), "transfer_in")

    def test_match_target_belongs_to_import_account(self) -> None:
        self.assertTrue(match_target_belongs_to_account({"account_id": "5"}, 5))
        self.assertFalse(match_target_belongs_to_account({"account_id": "6"}, 5))
        self.assertFalse(match_target_belongs_to_account(None, 5))

    def test_matched_transaction_amount_keeps_manual_amount_by_default(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-15.00",
            "match_amt_src_0": "manual",
        }), 0)

        self.assertEqual(matched_transaction_amount_cents(row, {"amount_cents": -1200}), -1200)

    def test_matched_transaction_amount_can_use_import_amount(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-15.00",
            "match_amt_src_0": "import",
        }), 0)

        self.assertEqual(matched_transaction_amount_cents(row, {"amount_cents": -1200}), -1500)

    def test_matched_transaction_payload_preserves_existing_blanks(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-15.00",
            "posted_at_0": "",
            "payee_0": "",
            "memo_0": "Imported memo",
            "fitid_0": "",
        }), 0)
        existing = {
            "posted_at": "2026-05-01",
            "payee": "Manual Payee",
            "memo": "Manual memo",
            "amount_cents": -1200,
            "fitid": "manual-fit",
        }

        self.assertEqual(
            matched_transaction_payload(row, existing, -1500),
            {
                "posted_at": "2026-05-01",
                "payee": "Manual Payee",
                "memo": "Imported memo",
                "amount_cents": -1500,
                "fitid": "manual-fit",
            },
        )

    def test_update_import_matched_transaction_edits_match_and_fitid(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-15.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Imported Payee",
            "orig_payee_0": "RAW PAYEE",
            "fitid_0": "fit-new",
            "match_tx_0": "42",
            "match_amt_src_0": "import",
            "exp_amount_0_7": "15.00",
        }), 0)
        edits = []
        existing_fitids = set()

        skip_reason = update_import_matched_transaction(
            account_id=5,
            row=row,
            form=MultiDict({"exp_amount_0_7": "15.00"}),
            existing_fitids=existing_fitids,
            get_transaction_func=lambda tx_id: {"id": tx_id, "account_id": 5, "amount_cents": -1200, "posted_at": "2026-05-01"},
            edit_transaction_func=lambda **kwargs: edits.append(kwargs),
            flash_func=lambda *args: None,
        )

        self.assertIsNone(skip_reason)
        self.assertEqual(edits[0]["tx_id"], 42)
        self.assertEqual(edits[0]["payload"]["amount_cents"], -1500)
        self.assertEqual(edits[0]["splits"], [{"envelope_id": 7, "amount_cents": 1500}])
        self.assertEqual(existing_fitids, {"fit-new"})

    def test_update_import_matched_transaction_passes_remainder_only_split_plan(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-15.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Imported Payee",
            "fitid_0": "fit-new",
            "match_tx_0": "42",
            "match_amt_src_0": "import",
            "exp_remainder_0": "8",
        }), 0)
        edits = []

        skip_reason = update_import_matched_transaction(
            account_id=5,
            row=row,
            form=MultiDict({"exp_remainder_0": "8"}),
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: {"id": tx_id, "account_id": 5, "amount_cents": -1200, "posted_at": "2026-05-01"},
            edit_transaction_func=lambda **kwargs: edits.append(kwargs),
            flash_func=lambda *args: None,
        )

        self.assertIsNone(skip_reason)
        self.assertEqual(edits[0]["splits"], None)
        self.assertEqual(edits[0]["remainder_envelope_id"], 8)
        self.assertEqual(edits[0]["remainder_amount_cents"], 1500)

    def test_update_import_matched_transaction_returns_skip_for_wrong_account(self) -> None:
        row = parse_import_row_form(MultiDict({"amount_1": "-15.00", "match_tx_1": "42"}), 1)
        flashes = []
        edits = []

        skip_reason = update_import_matched_transaction(
            account_id=5,
            row=row,
            form=MultiDict(),
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: {"id": tx_id, "account_id": 6, "amount_cents": -1200},
            edit_transaction_func=lambda **kwargs: edits.append(kwargs),
            flash_func=lambda message, category: flashes.append((message, category)),
        )

        self.assertEqual(skip_reason, "Row 2: match target not found")
        self.assertEqual(flashes, [("Match target 42 not found in this account; skipped row 2.", "warning")])
        self.assertEqual(edits, [])

    def test_create_import_standard_transaction_from_row_creates_expense(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-15.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Coffee",
            "orig_payee_0": "RAW COFFEE",
            "exp_remainder_0": "8",
        }), 0)
        expenses = []
        incomes = []

        create_import_standard_transaction_from_row(
            account_id=5,
            row=row,
            amount_cents=-1500,
            splits=[{"envelope_id": 7, "amount_cents": 1500}],
            form=MultiDict({"exp_remainder_0": "8"}),
            create_expense_func=lambda **kwargs: expenses.append(kwargs),
            create_income_func=lambda **kwargs: incomes.append(kwargs),
        )

        self.assertEqual(len(expenses), 1)
        self.assertEqual(incomes, [])
        self.assertEqual(expenses[0]["payload"]["account_id"], 5)
        self.assertEqual(expenses[0]["remainder_envelope_id"], 8)

    def test_create_import_standard_transaction_from_row_creates_income_without_learning(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_1": "15.00",
            "posted_at_1": "2026-05-12",
            "payee_1": "Deposit",
            "inc_remainder_1": "9",
        }), 1)
        expenses = []
        incomes = []

        create_import_standard_transaction_from_row(
            account_id=5,
            row=row,
            amount_cents=1500,
            splits=[{"envelope_id": 7, "amount_cents": 1500}],
            form=MultiDict({"inc_remainder_1": "9"}),
            create_expense_func=lambda **kwargs: expenses.append(kwargs),
            create_income_func=lambda **kwargs: incomes.append(kwargs),
        )

        self.assertEqual(expenses, [])
        self.assertEqual(len(incomes), 1)
        self.assertEqual(incomes[0]["payload"]["payee"], "Deposit")
        self.assertEqual(incomes[0]["remainder_envelope_id"], 9)

    def test_commit_import_rows_processes_selected_rows_and_tracks_matches(self) -> None:
        form = MultiDict({
            "count": "2",
            "row_0": "on",
            "amount_0": "1.00",
            "fitid_0": "fit-1",
            "row_1": "on",
            "amount_1": "12.00",
            "fitid_1": "fit-2",
            "match_tx_1": "42",
        })
        edits = []

        class Logger:
            def info(self, *args):
                raise AssertionError("unexpected info log")

            def exception(self, *args):
                raise AssertionError("unexpected exception log")

        tally, matched_ids = commit_import_rows(
            account_id=5,
            account={"account_type": "bank"},
            form=form,
            count=2,
            existing_fitids={"fit-1"},
            get_transaction_func=lambda tx_id: {"id": tx_id, "account_id": 5, "amount_cents": 1200},
            edit_transaction_func=lambda **kwargs: edits.append(kwargs),
            get_account_func=lambda account_id: None,
            create_transfer_func=lambda **kwargs: None,
            create_expense_func=lambda **kwargs: None,
            create_income_func=lambda **kwargs: None,
            logger=Logger(),
            flash_func=lambda *args: None,
        )

        self.assertEqual(tally.imported, 1)
        self.assertEqual(tally.skipped, 1)
        self.assertEqual(tally.skipped_reasons, [])
        self.assertEqual(matched_ids, {42})
        self.assertEqual(edits[0]["tx_id"], 42)

    def test_commit_import_rows_records_parse_errors_with_row_context(self) -> None:
        form = MultiDict({"count": "1", "row_0": "on", "amount_0": "bad"})
        logs = []

        class Logger:
            def info(self, *args):
                logs.append(("info", args))

            def exception(self, *args):
                logs.append(("exception", args))

        tally, matched_ids = commit_import_rows(
            account_id=5,
            account={"account_type": "bank"},
            form=form,
            count=1,
            existing_fitids=set(),
            get_transaction_func=lambda tx_id: None,
            edit_transaction_func=lambda **kwargs: None,
            get_account_func=lambda account_id: None,
            create_transfer_func=lambda **kwargs: None,
            create_expense_func=lambda **kwargs: None,
            create_income_func=lambda **kwargs: None,
            logger=Logger(),
            flash_func=lambda *args: None,
        )

        self.assertEqual(tally.imported, 0)
        self.assertEqual(tally.skipped, 1)
        self.assertIn("Row 1 amount", tally.skipped_reasons[0])
        self.assertEqual(matched_ids, set())
        self.assertEqual(logs[0][0], "info")

    def test_commit_import_row_skips_duplicate_fitids(self) -> None:
        row = parse_import_row_form(MultiDict({"amount_0": "1.00", "fitid_0": "fit-1"}), 0)
        matched_ids = set()

        result = commit_import_row(
            account_id=5,
            account={"account_type": "bank"},
            row=row,
            form=MultiDict(),
            existing_fitids={"fit-1"},
            matched_ids=matched_ids,
            get_transaction_func=lambda tx_id: None,
            edit_transaction_func=lambda **kwargs: None,
            get_account_func=lambda account_id: None,
            create_transfer_func=lambda **kwargs: None,
            create_expense_func=lambda **kwargs: None,
            create_income_func=lambda **kwargs: None,
            flash_func=lambda *args: None,
        )

        self.assertFalse(result.imported)
        self.assertTrue(result.skipped)
        self.assertIsNone(result.skip_reason)
        self.assertEqual(matched_ids, set())

    def test_commit_import_row_updates_matched_transactions(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "12.00",
            "fitid_0": "fit-2",
            "match_tx_0": "42",
        }), 0)
        matched_ids = set()
        edits = []

        result = commit_import_row(
            account_id=5,
            account={"account_type": "bank"},
            row=row,
            form=MultiDict(),
            existing_fitids=set(),
            matched_ids=matched_ids,
            get_transaction_func=lambda tx_id: {"id": tx_id, "account_id": 5, "amount_cents": 1200},
            edit_transaction_func=lambda **kwargs: edits.append(kwargs),
            get_account_func=lambda account_id: None,
            create_transfer_func=lambda **kwargs: None,
            create_expense_func=lambda **kwargs: None,
            create_income_func=lambda **kwargs: None,
            flash_func=lambda *args: None,
        )

        self.assertTrue(result.imported)
        self.assertFalse(result.skipped)
        self.assertEqual(matched_ids, {42})
        self.assertEqual(edits[0]["tx_id"], 42)

    def test_create_import_transaction_from_row_routes_standard_rows(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "15.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Deposit",
            "inc_remainder_0": "9",
        }), 0)
        incomes = []
        transfers = []

        reason = create_import_transaction_from_row(
            account_id=5,
            account={"account_type": "bank"},
            row=row,
            form=MultiDict({"inc_remainder_0": "9"}),
            get_account_func=lambda account_id: {"id": account_id},
            create_transfer_func=lambda **kwargs: transfers.append(kwargs),
            create_expense_func=lambda **kwargs: None,
            create_income_func=lambda **kwargs: incomes.append(kwargs),
            flash_func=lambda *args: None,
        )

        self.assertIsNone(reason)
        self.assertEqual(transfers, [])
        self.assertEqual(len(incomes), 1)
        self.assertEqual(incomes[0]["payload"]["payee"], "Deposit")
        self.assertEqual(incomes[0]["remainder_envelope_id"], 9)

    def test_create_import_transaction_from_row_routes_transfer_rows(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "10.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Transfer",
            "is_transfer_0": "on",
            "transfer_account_0": "7",
            "trf_amt_0_3": "10.00",
        }), 0)
        transfers = []

        reason = create_import_transaction_from_row(
            account_id=5,
            account={"account_type": "bank"},
            row=row,
            form=MultiDict({"trf_amt_0_3": "10.00"}),
            get_account_func=lambda account_id: {"id": account_id, "account_type": "bank"},
            create_transfer_func=lambda **kwargs: transfers.append(kwargs),
            create_expense_func=lambda **kwargs: None,
            create_income_func=lambda **kwargs: None,
            flash_func=lambda *args: None,
        )

        self.assertIsNone(reason)
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0]["payload"]["from_account_id"], 7)
        self.assertEqual(transfers[0]["payload"]["to_account_id"], 5)
        self.assertEqual(transfers[0]["out_splits"], [{"envelope_id": 3, "amount_cents": 1000}])

    def test_standard_transaction_payload_uses_import_row_fields(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "42.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Deposit",
            "memo_0": "Check",
            "fitid_0": "fit-42",
        }), 0)

        self.assertEqual(
            standard_transaction_payload(5, row),
            {
                "account_id": 5,
                "posted_at": "2026-05-12",
                "payee": "Deposit",
                "memo": "Check",
                "fitid": "fit-42",
                "amount_cents": 4200,
            },
        )

    def test_import_transfer_type_helpers_identify_transfer_direction(self) -> None:
        self.assertTrue(is_import_transfer_transaction_type("transfer_in"))
        self.assertTrue(is_import_transfer_transaction_type("transfer_out"))
        self.assertFalse(is_import_transfer_transaction_type("expense"))
        self.assertFalse(import_transfer_is_out("transfer_in"))
        self.assertTrue(import_transfer_is_out("transfer_out"))

    def test_import_transfer_splits_maps_outgoing_transfer_legs(self) -> None:
        from_leg = [{"envelope_id": 1, "amount_cents": 700}]
        other_leg = [{"envelope_id": 2, "amount_cents": 700}]

        split_plan = import_transfer_splits(
            is_out=True,
            from_leg_splits=from_leg,
            other_leg_splits=other_leg,
            other_account={"account_type": "bank"},
        )

        self.assertEqual(split_plan.out_splits, from_leg)
        self.assertEqual(split_plan.in_splits, other_leg)
        self.assertFalse(split_plan.allow_unallocated_in)

    def test_import_transfer_splits_maps_incoming_transfer_legs_and_loan_unallocated(self) -> None:
        from_leg = [{"envelope_id": 1, "amount_cents": 700}]
        other_leg = [{"envelope_id": 2, "amount_cents": 700}]

        split_plan = import_transfer_splits(
            is_out=False,
            from_leg_splits=from_leg,
            other_leg_splits=other_leg,
            other_account={"account_type": "loan"},
        )

        self.assertEqual(split_plan.out_splits, other_leg)
        self.assertEqual(split_plan.in_splits, from_leg)
        self.assertTrue(split_plan.allow_unallocated_in)

    def test_create_import_transfer_from_row_creates_transfer_with_split_plan(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-10.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Transfer",
            "memo_0": "Move money",
            "fitid_0": "fit-transfer",
            "transfer_account_0": "2",
            "trf_amt_0_9": "4.00",
            "trf_remainder_0": "10",
        }), 0)
        created = []

        reason = create_import_transfer_from_row(
            account_id=1,
            row=row,
            amount_cents=-1000,
            is_out=True,
            from_leg_splits=[{"envelope_id": 7, "amount_cents": 1000}],
            form=MultiDict({"trf_amt_0_9": "4.00", "trf_remainder_0": "10"}),
            get_account_func=lambda account_id: {"id": account_id, "account_type": "loan"},
            create_transfer_func=lambda **kwargs: created.append(kwargs),
            flash_func=lambda *args: None,
        )

        self.assertIsNone(reason)
        self.assertEqual(created[0]["payload"]["from_account_id"], 1)
        self.assertEqual(created[0]["payload"]["to_account_id"], 2)
        self.assertEqual(created[0]["payload"]["out_fitid"], "fit-transfer")
        self.assertIsNone(created[0]["payload"]["in_fitid"])
        self.assertEqual(created[0]["out_splits"], [{"envelope_id": 7, "amount_cents": 1000}])
        self.assertEqual(created[0]["in_splits"], [
            {"envelope_id": 9, "amount_cents": 400},
            {"envelope_id": 10, "amount_cents": 600},
        ])
        self.assertTrue(created[0]["allow_unallocated_in"])

    def test_create_import_transfer_from_row_passes_independent_remainder_metadata(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-10.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Transfer",
            "memo_0": "Move money",
            "fitid_0": "fit-transfer",
            "transfer_account_0": "2",
        }), 0)
        created = []

        reason = create_import_transfer_from_row(
            account_id=1,
            row=row,
            amount_cents=-1000,
            is_out=True,
            from_leg_split_plan=ImportSplitPlan(
                splits=[
                    {"envelope_id": 7, "amount_cents": -750},
                    {"envelope_id": 8, "amount_cents": -250},
                ],
                remainder_envelope_id=8,
                remainder_amount_cents=-250,
            ),
            form=MultiDict({"trf_amt_0_9": "4.00", "trf_remainder_0": "10"}),
            get_account_func=lambda account_id: {"id": account_id, "account_type": "bank"},
            create_transfer_func=lambda **kwargs: created.append(kwargs),
            flash_func=lambda *args: None,
        )

        self.assertIsNone(reason)
        self.assertEqual(created[0]["out_remainder_envelope_id"], 8)
        self.assertEqual(created[0]["out_remainder_amount_cents"], -250)
        self.assertEqual(created[0]["in_remainder_envelope_id"], 10)
        self.assertEqual(created[0]["in_remainder_amount_cents"], 600)
        self.assertEqual(created[0]["out_splits"], [
            {"envelope_id": 7, "amount_cents": -750},
            {"envelope_id": 8, "amount_cents": -250},
        ])
        self.assertEqual(created[0]["in_splits"], [
            {"envelope_id": 9, "amount_cents": 400},
            {"envelope_id": 10, "amount_cents": 600},
        ])

    def test_create_import_transfer_from_row_returns_skip_reason_without_other_account(self) -> None:
        row = parse_import_row_form(MultiDict({"amount_1": "-10.00"}), 1)
        flashes = []
        created = []

        reason = create_import_transfer_from_row(
            account_id=1,
            row=row,
            amount_cents=-1000,
            is_out=True,
            from_leg_splits=[],
            form=MultiDict(),
            get_account_func=lambda account_id: {"id": account_id},
            create_transfer_func=lambda **kwargs: created.append(kwargs),
            flash_func=lambda message, category: flashes.append((message, category)),
        )

        self.assertEqual(reason, "Row 2: transfer needs another account")
        self.assertEqual(flashes, [("Transfer row 2 needs a destination/source account.", "warning")])
        self.assertEqual(created, [])

    def test_import_row_duplicate_checks_fitid_presence(self) -> None:
        existing_fitids = {"fit-1"}
        duplicate = parse_import_row_form(MultiDict({"amount_0": "1.00", "fitid_0": "fit-1"}), 0)
        new = parse_import_row_form(MultiDict({"amount_0": "1.00", "fitid_0": "fit-2"}), 0)
        blank = parse_import_row_form(MultiDict({"amount_0": "1.00", "fitid_0": ""}), 0)

        self.assertTrue(import_row_is_duplicate(duplicate, existing_fitids))
        self.assertFalse(import_row_is_duplicate(new, existing_fitids))
        self.assertFalse(import_row_is_duplicate(blank, existing_fitids))

    def test_remember_imported_fitid_only_adds_present_fitids(self) -> None:
        existing_fitids: set[str] = set()
        row = parse_import_row_form(MultiDict({"amount_0": "1.00", "fitid_0": "fit-3"}), 0)
        blank = parse_import_row_form(MultiDict({"amount_1": "1.00", "fitid_1": ""}), 1)

        remember_imported_fitid(row, existing_fitids)
        remember_imported_fitid(blank, existing_fitids)

        self.assertEqual(existing_fitids, {"fit-3"})

    def test_transfer_transaction_payload_sets_directional_accounts(self) -> None:
        row = parse_import_row_form(MultiDict({
            "amount_0": "-10.00",
            "posted_at_0": "2026-05-12",
            "payee_0": "Transfer",
            "memo_0": "Move money",
            "fitid_0": "fit-transfer",
        }), 0)

        self.assertEqual(
            transfer_transaction_payload(1, 2, row, is_out=True),
            {
                "amount_cents": 1000,
                "date": "2026-05-12",
                "memo": "Move money",
                "from_account_id": 1,
                "to_account_id": 2,
                "out_fitid": "fit-transfer",
                "in_fitid": None,
                "payee": "Transfer",
            },
        )
        incoming_payload = transfer_transaction_payload(1, 2, row, is_out=False)
        self.assertEqual(incoming_payload["from_account_id"], 2)
        self.assertEqual(incoming_payload["to_account_id"], 1)
        self.assertIsNone(incoming_payload["out_fitid"])
        self.assertEqual(incoming_payload["in_fitid"], "fit-transfer")

    def test_ignored_transaction_ids_filters_invalid_and_matched_ids(self) -> None:
        form = MultiDict([
            ("ignore_tx[]", "7"),
            ("ignore_tx[]", "not-an-id"),
            ("ignore_tx[]", "8"),
            ("ignore_tx[]", "9"),
        ])

        self.assertEqual(ignored_transaction_ids(form, {8}), [7, 9])

    def test_flash_skip_helpers_emit_warning_and_return_reason(self) -> None:
        flashes = []

        invalid_reason = flash_invalid_match_skip(2, 42, lambda message, category: flashes.append((message, category)))
        transfer_reason = flash_missing_transfer_account_skip(3, lambda message, category: flashes.append((message, category)))

        self.assertEqual(invalid_reason, "Row 3: match target not found")
        self.assertEqual(transfer_reason, "Row 4: transfer needs another account")
        self.assertEqual(flashes, [
            ("Match target 42 not found in this account; skipped row 3.", "warning"),
            ("Transfer row 4 needs a destination/source account.", "warning"),
        ])

    def test_ignored_transaction_payload_marks_tx_as_ignored(self) -> None:
        self.assertEqual(ignored_transaction_payload(7), {"fitid": "Ignore-7", "ignore_match": 1})

    def test_finalize_import_commit_marks_ignored_and_flashes_result(self) -> None:
        tally = ImportCommitTally(imported=2, skipped=1, skipped_reasons=["one skipped"])
        edits = []
        flashes = []

        message, category = finalize_import_commit(
            tally=tally,
            form=MultiDict([("ignore_tx[]", "1"), ("ignore_tx[]", "2")]),
            matched_ids={2},
            edit_transaction_func=lambda **kwargs: edits.append(kwargs),
            logger=None,
            flash_func=lambda message, category: flashes.append((message, category)),
        )

        self.assertEqual(message, "Imported 2 transaction(s). Skipped 1. (one skipped)")
        self.assertEqual(category, "warning")
        self.assertEqual(flashes, [(message, category)])
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["tx_id"], 1)
        self.assertEqual(edits[0]["payload"], {"fitid": "Ignore-1", "ignore_match": 1})

    def test_finalize_import_commit_success_flash_includes_undo_session(self) -> None:
        tally = ImportCommitTally(imported=2, import_session_id=99)
        flashes = []

        message, category = finalize_import_commit(
            tally=tally,
            form=MultiDict(),
            matched_ids=set(),
            edit_transaction_func=lambda **kwargs: self.fail("no ignored rows expected"),
            logger=None,
            flash_func=lambda message, category: flashes.append((message, category)),
        )

        self.assertEqual(message, "Imported 2 transaction(s). Skipped 0.")
        self.assertEqual(category, "success")
        self.assertEqual(flashes, [({
            "text": message,
            "import_undo_session_id": 99,
        }, "success")])

    def test_mark_ignored_transactions_edits_unmatched_ids_and_reports_failures(self) -> None:
        form = MultiDict([
            ("ignore_tx[]", "7"),
            ("ignore_tx[]", "8"),
            ("ignore_tx[]", "9"),
        ])
        edits = []
        logged = []

        class Logger:
            def exception(self, message, *args):
                logged.append((message, args))

        def edit_transaction(**kwargs):
            edits.append(kwargs)
            if kwargs["tx_id"] == 9:
                raise RuntimeError("ignore boom")

        marked, failed = mark_ignored_transactions(form, {8}, edit_transaction, Logger())

        self.assertEqual(marked, 1)
        self.assertEqual(failed, 1)
        self.assertEqual(edits[0], {"tx_id": 7, "payload": {"fitid": "Ignore-7", "ignore_match": 1}, "splits": None})
        self.assertEqual(edits[1]["tx_id"], 9)
        self.assertEqual(logged[0][0], "Failed to mark tx %s ignored: %s")
        self.assertEqual(logged[0][1][0], 9)
        self.assertIsInstance(logged[0][1][1], RuntimeError)

    def test_skip_message_helpers_include_one_based_row_numbers(self) -> None:
        self.assertEqual(
            invalid_match_flash_message(2, 42),
            "Match target 42 not found in this account; skipped row 3.",
        )
        self.assertEqual(invalid_match_skip_reason(2), "Row 3: match target not found")
        self.assertEqual(
            missing_transfer_account_flash_message(2),
            "Transfer row 3 needs a destination/source account.",
        )
        self.assertEqual(missing_transfer_account_skip_reason(2), "Row 3: transfer needs another account")
        self.assertEqual(
            unexpected_import_error_skip_reason(2, RuntimeError("boom")),
            "Row 3: unexpected error (RuntimeError)",
        )

    def test_import_result_flash_formats_success(self) -> None:
        self.assertEqual(
            import_result_flash(3, 0, []),
            ("Imported 3 transaction(s). Skipped 0.", "success"),
        )

    def test_import_result_flash_formats_warning_with_limited_reasons(self) -> None:
        message, category = import_result_flash(2, 4, ["one", "two", "three", "four"])

        self.assertEqual(category, "warning")
        self.assertEqual(
            message,
            "Imported 2 transaction(s). Skipped 4. (one; two; three; plus 1 more)",
        )

    def test_collect_import_row_splits_uses_expense_quick_assign(self) -> None:
        form = MultiDict({"exp_single_0": "7"})

        splits = collect_import_row_splits(form, 0, -1234)

        self.assertEqual(splits, [{"envelope_id": 7, "amount_cents": 1234}])

    def test_collect_import_row_splits_adds_remainder(self) -> None:
        form = MultiDict({
            "inc_amount_2_10": "4.00",
            "inc_remainder_2": "11",
        })

        splits = collect_import_row_splits(form, 2, 1000)

        self.assertEqual(
            splits,
            [
                {"envelope_id": 10, "amount_cents": 400},
                {"envelope_id": 11, "amount_cents": 600},
            ],
        )

    def test_collect_import_row_split_plan_returns_remainder_metadata(self) -> None:
        form = MultiDict({
            "exp_amount_0_5": "4.00",
            "exp_remainder_0": "6",
        })

        plan = collect_import_row_split_plan(form, 0, -1000)

        self.assertEqual(
            plan.splits,
            [
                {"envelope_id": 5, "amount_cents": 400},
                {"envelope_id": 6, "amount_cents": 600},
            ],
        )
        self.assertEqual(plan.remainder_envelope_id, 6)
        self.assertEqual(plan.remainder_amount_cents, 600)

    def test_collect_import_row_splits_returns_empty_when_not_fully_allocated(self) -> None:
        form = MultiDict({"exp_amount_1_7": "3.00"})

        splits = collect_import_row_splits(form, 1, -1000)

        self.assertEqual(splits, [])

    def test_collect_import_row_splits_rejects_invalid_money(self) -> None:
        form = MultiDict({"exp_amount_0_7": "not-money"})

        with self.assertRaises(ValueError):
            collect_import_row_splits(form, 0, -1000)

    def test_collect_import_creation_splits_prefers_transfer_from_leg(self) -> None:
        form = MultiDict({
            "trf_from_amt_0_3": "6.00",
            "trf_from_remainder_0": "4",
            "exp_amount_0_5": "10.00",
        })

        splits = collect_import_creation_splits(form, 0, -1000)

        self.assertEqual(
            splits,
            [
                {"envelope_id": 3, "amount_cents": 600},
                {"envelope_id": 4, "amount_cents": 400},
            ],
        )

    def test_collect_import_creation_split_plan_prefers_transfer_remainder_metadata(self) -> None:
        form = MultiDict({
            "trf_from_amt_0_3": "6.00",
            "trf_from_remainder_0": "4",
            "exp_amount_0_5": "10.00",
        })

        plan = collect_import_creation_split_plan(form, 0, -1000)

        self.assertEqual(plan.remainder_envelope_id, 4)
        self.assertEqual(plan.remainder_amount_cents, 400)
        self.assertEqual(plan.splits[-1], {"envelope_id": 4, "amount_cents": 400})

    def test_collect_import_creation_splits_falls_back_to_normal_splits(self) -> None:
        form = MultiDict({
            "exp_amount_0_5": "4.00",
            "exp_remainder_0": "6",
        })

        splits = collect_import_creation_splits(form, 0, -1000)

        self.assertEqual(
            splits,
            [
                {"envelope_id": 5, "amount_cents": 400},
                {"envelope_id": 6, "amount_cents": 600},
            ],
        )

    def test_collect_transfer_from_splits_uses_from_remainder(self) -> None:
        form = MultiDict({
            "trf_from_amt_0_7": "6.25",
            "trf_from_remainder_0": "8",
        })

        splits = collect_import_transfer_from_splits(form, 0, -1000)

        self.assertEqual(
            splits,
            [
                {"envelope_id": 7, "amount_cents": 625},
                {"envelope_id": 8, "amount_cents": 375},
            ],
        )

    def test_collect_transfer_from_split_plan_returns_remainder_metadata(self) -> None:
        form = MultiDict({
            "trf_from_amt_0_7": "6.25",
            "trf_from_remainder_0": "8",
        })

        plan = collect_import_transfer_from_split_plan(form, 0, -1000)

        self.assertEqual(plan.remainder_envelope_id, 8)
        self.assertEqual(plan.remainder_amount_cents, 375)

    def test_collect_transfer_splits_uses_other_leg_remainder(self) -> None:
        form = MultiDict({
            "trf_amt_3_12": "1.50",
            "trf_remainder_3": "13",
        })

        splits = collect_import_transfer_splits(form, 3, 200)

        self.assertEqual(
            splits,
            [
                {"envelope_id": 12, "amount_cents": 150},
                {"envelope_id": 13, "amount_cents": 50},
            ],
        )

    def test_collect_transfer_split_plan_returns_other_leg_remainder_metadata(self) -> None:
        form = MultiDict({
            "trf_amt_3_12": "1.50",
            "trf_remainder_3": "13",
        })

        plan = collect_import_transfer_split_plan(form, 3, 200)

        self.assertEqual(plan.remainder_envelope_id, 13)
        self.assertEqual(plan.remainder_amount_cents, 50)

    def test_collect_import_row_splits_accepts_mixed_signed_income(self) -> None:
        form = MultiDict({
            "inc_amount_0_10": "20.00",
            "inc_amount_0_11": "-7.66",
        })

        splits = collect_import_row_splits(form, 0, 1234)

        self.assertEqual(
            splits,
            [
                {"envelope_id": 10, "amount_cents": 2000},
                {"envelope_id": 11, "amount_cents": -766},
            ],
        )

    def test_collect_import_transfer_from_splits_accepts_fictional_mixed_signed_example(self) -> None:
        form = MultiDict({
            "trf_from_amt_0_10": "8.00",
            "trf_from_amt_0_11": "9.00",
            "trf_from_amt_0_12": "-4.66",
        })

        splits = collect_import_transfer_from_splits(form, 0, 1234)

        self.assertEqual(sum(split["amount_cents"] for split in splits), 1234)
        self.assertIn({"envelope_id": 12, "amount_cents": -466}, splits)
        self.assertIn({"envelope_id": 11, "amount_cents": 900}, splits)

    def test_collect_import_transfer_splits_accepts_signed_other_leg_outflow(self) -> None:
        form = MultiDict({
            "trf_amt_3_12": "-3.50",
            "trf_amt_3_13": "1.50",
        })

        splits = collect_import_transfer_splits(form, 3, 200)

        self.assertEqual(
            splits,
            [
                {"envelope_id": 12, "amount_cents": -350},
                {"envelope_id": 13, "amount_cents": 150},
            ],
        )
