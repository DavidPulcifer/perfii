import json
import unittest

from app.db import get_meta_db
from app.repositories import accounts_repo, envelopes_repo, import_matching_rules_repo
from app.services.import_matching_rule_service import (
    build_import_matching_rule_prefills,
    parse_rule_form,
    select_import_matching_rules,
)
from tests.helpers import FinanceAppTestCase
from werkzeug.datastructures import MultiDict


class ImportMatchingRuleServiceTests(unittest.TestCase):
    def _rule(self, rule_id, condition, action, *, priority=100):
        return {
            "id": rule_id,
            "account_id": 1,
            "name": f"Rule {rule_id}",
            "enabled": 1,
            "priority": priority,
            "condition_json": condition,
            "action_json": action,
        }

    def test_selects_rule_by_text_amount_and_direction(self) -> None:
        row = {"payee": "Amazon Marketplace", "memo": "Order 123", "amount_cents": -2599}
        rule = self._rule(
            7,
            {"direction": "expense", "field": "text", "operator": "contains", "value": "amazon", "amount_min_cents": 2000},
            {"payee": "Amazon", "transaction_type": "expense", "single_envelope_id": 12},
        )

        selection = select_import_matching_rules(row, 0, 1, [rule])

        self.assertFalse(selection.conflict)
        self.assertEqual(selection.actions["payee"], "Amazon")
        self.assertEqual(selection.actions["single_envelope_id"], 12)

    def test_merges_non_conflicting_matching_rules(self) -> None:
        row = {"payee": "Payroll Deposit", "memo": "", "amount_cents": 100000}
        rules = [
            self._rule(1, {"direction": "income", "field": "payee", "operator": "contains", "value": "payroll"}, {"payee": "Employer"}),
            self._rule(2, {"direction": "income", "field": "payee", "operator": "contains", "value": "payroll"}, {"single_envelope_id": 3}),
        ]

        selection = select_import_matching_rules(row, 0, 1, rules)

        self.assertFalse(selection.conflict)
        self.assertEqual(selection.actions["payee"], "Employer")
        self.assertEqual(selection.actions["single_envelope_id"], 3)

    def test_conflicting_actions_are_withheld(self) -> None:
        row = {"payee": "Coffee Shop", "memo": "", "amount_cents": -500}
        rules = [
            self._rule(1, {"direction": "expense", "field": "payee", "operator": "contains", "value": "coffee"}, {"payee": "Coffee A"}),
            self._rule(2, {"direction": "expense", "field": "payee", "operator": "contains", "value": "coffee"}, {"payee": "Coffee B"}),
        ]

        selection = select_import_matching_rules(row, 0, 1, rules)

        self.assertTrue(selection.conflict)
        self.assertEqual(selection.conflict_reason, "conflicting_payee")

    def test_build_prefills_uses_existing_import_review_shapes(self) -> None:
        row = {"payee": "Grocery Store", "memo": "weekly", "amount_cents": -4567}
        rule = self._rule(
            9,
            {"direction": "expense", "field": "payee", "operator": "contains", "value": "grocery"},
            {"payee": "Groceries", "transaction_type": "expense", "single_envelope_id": 4},
        )

        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
        )

        self.assertEqual(prefills["import_prefills"][0]["prediction_type"], "manual_rule")
        self.assertEqual(prefills["import_prefills"][0]["single_envelope_id"], 4)
        self.assertEqual(prefills["payee_prefills"][0]["canonical_payee"], "Groceries")

    def test_parse_rule_form_rejects_empty_condition_or_action(self) -> None:
        data, errors = parse_rule_form(MultiDict({"name": "Empty", "account_id": "1"}))

        self.assertIsNone(data)
        self.assertIn("Add at least one match condition.", errors)
        self.assertIn("Add at least one rule action.", errors)

    def test_parse_rule_form_accepts_split_remainder_action_payload(self) -> None:
        payload = {
            "transaction_type": "expense",
            "splits": [
                {"envelope_id": 10, "amount_cents": 2500, "amount_mode": "absolute"},
            ],
            "remainder_envelope_id": 11,
            "target_amount_cents": 7500,
        }

        data, errors = parse_rule_form(
            MultiDict({
                "name": "Split utilities",
                "account_id": "1",
                "direction": "expense",
                "match_field": "payee",
                "match_operator": "contains",
                "match_value": "utility",
                "action_split_remainder_json": json.dumps(payload),
            }),
            list_envelopes_func=lambda **_: [
                {"id": 10, "name": "Power", "locked_account_id": None, "archived_at": None},
                {"id": 11, "name": "Home", "locked_account_id": 1, "archived_at": None},
            ],
        )

        self.assertEqual(errors, [])
        split_action = data["action_json"]["split_remainder"]
        self.assertEqual(split_action["transaction_type"], "expense")
        self.assertEqual(split_action["splits"], [
            {"envelope_id": 10, "amount_cents": -2500, "amount_mode": "absolute"},
        ])
        self.assertEqual(split_action["remainder_envelope_id"], 11)
        self.assertEqual(split_action["target_amount_cents"], -7500)

    def test_split_remainder_action_prefills_existing_import_review_shape(self) -> None:
        row = {"payee": "Utility Co", "memo": "", "amount_cents": -7500}
        rule = self._rule(
            11,
            {"direction": "expense", "field": "payee", "operator": "contains", "value": "utility"},
            {
                "split_remainder": {
                    "transaction_type": "expense",
                    "splits": [{"envelope_id": 10, "amount_cents": -2500, "amount_mode": "signed"}],
                    "remainder_envelope_id": 11,
                },
            },
        )

        selection = select_import_matching_rules(row, 0, 1, [rule])
        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
            list_envelopes_func=lambda **_: [
                {"id": 10, "name": "Fixed", "locked_account_id": None, "archived_at": None},
                {"id": 11, "name": "Remainder", "locked_account_id": 1, "archived_at": None},
            ],
        )

        self.assertFalse(selection.conflict)
        self.assertEqual(selection.actions["split_remainder"]["remainder_envelope_id"], 11)
        self.assertEqual(len(prefills["import_prefills"]), 1)
        prefill = prefills["import_prefills"][0]
        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transaction_type"], "expense")
        self.assertEqual(prefill["splits"], [{"envelope_id": 10, "amount_cents": -2500}])
        self.assertEqual(prefill["remainder_envelope_id"], 11)
        self.assertEqual(prefill["remainder_amount_cents"], -5000)
        self.assertEqual(prefill["prediction_debug"]["decision"], "prefill")
        self.assertEqual(prefills["payee_prefills"], [])

    def test_split_remainder_action_withholds_invalid_row_amount(self) -> None:
        row = {"payee": "Utility Co", "memo": "", "amount_cents": -8000}
        rule = self._rule(
            12,
            {"direction": "expense", "field": "payee", "operator": "contains", "value": "utility"},
            {
                "split_remainder": {
                    "transaction_type": "expense",
                    "splits": [{"envelope_id": 10, "amount_cents": -2500, "amount_mode": "signed"}],
                    "remainder_envelope_id": 11,
                    "target_amount_cents": -7500,
                },
            },
        )

        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
            list_envelopes_func=lambda **_: [
                {"id": 10, "name": "Fixed", "locked_account_id": None, "archived_at": None},
                {"id": 11, "name": "Remainder", "locked_account_id": None, "archived_at": None},
            ],
        )

        self.assertEqual(len(prefills["import_prefills"]), 1)
        self.assertFalse(prefills["import_prefills"][0]["prefill"])
        self.assertIn("manual_rule_split_remainder_amount_mismatch", prefills["import_prefills"][0]["debug_reason_codes"])

    def test_split_remainder_action_withholds_unavailable_envelope(self) -> None:
        row = {"payee": "Utility Co", "memo": "", "amount_cents": -7500}
        rule = self._rule(
            13,
            {"direction": "expense", "field": "payee", "operator": "contains", "value": "utility"},
            {
                "split_remainder": {
                    "transaction_type": "expense",
                    "splits": [{"envelope_id": 10, "amount_cents": -2500, "amount_mode": "signed"}],
                    "remainder_envelope_id": 11,
                },
            },
        )

        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
            list_envelopes_func=lambda **_: [
                {"id": 10, "name": "Fixed", "locked_account_id": None, "archived_at": None},
                {"id": 11, "name": "Archived", "locked_account_id": None, "archived_at": "2026-01-01"},
            ],
        )

        self.assertEqual(len(prefills["import_prefills"]), 1)
        self.assertFalse(prefills["import_prefills"][0]["prefill"])
        self.assertIn("manual_rule_split_remainder_unavailable_envelope", prefills["import_prefills"][0]["debug_reason_codes"])

    def test_parse_rule_form_accepts_transfer_action_payload(self) -> None:
        payload = {
            "transaction_type": "expense",
            "other_account_id": 2,
            "current_account_splits": [{"envelope_id": 10, "amount_cents": 4000}],
            "current_account_remainder_envelope_id": 11,
            "other_account_splits": [{"envelope_id": 20, "amount_cents": 4000}],
            "other_account_remainder_envelope_id": 21,
            "target_amount_cents": 10000,
        }

        data, errors = parse_rule_form(
            MultiDict({
                "name": "Transfer rule",
                "account_id": "1",
                "direction": "expense",
                "match_field": "memo",
                "match_operator": "contains",
                "match_value": "savings",
                "action_transfer_json": json.dumps(payload),
            }),
            list_accounts_func=lambda: [{"id": 1, "name": "Checking"}, {"id": 2, "name": "Savings"}],
            list_envelopes_func=lambda **_: [
                {"id": 10, "locked_account_id": 1, "archived_at": None},
                {"id": 11, "locked_account_id": None, "archived_at": None},
                {"id": 20, "locked_account_id": 2, "archived_at": None},
                {"id": 21, "locked_account_id": None, "archived_at": None},
            ],
        )

        self.assertEqual(errors, [])
        transfer = data["action_json"]["transfer"]
        self.assertEqual(transfer["other_account_id"], 2)
        self.assertEqual(transfer["target_amount_cents"], -10000)
        self.assertEqual(transfer["current_account_remainder_envelope_id"], 11)

    def test_transfer_action_prefills_existing_import_review_transfer_shape(self) -> None:
        row = {"payee": "Online Transfer", "memo": "savings", "amount_cents": -10000}
        rule = self._rule(
            21,
            {"direction": "expense", "field": "memo", "operator": "contains", "value": "savings"},
            {
                "transfer": {
                    "transaction_type": "expense",
                    "other_account_id": 2,
                    "current_account_splits": [{"envelope_id": 10, "amount_cents": 4000}],
                    "current_account_remainder_envelope_id": 11,
                    "other_account_splits": [{"envelope_id": 20, "amount_cents": 10000}],
                    "target_amount_cents": -10000,
                },
            },
        )

        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
            list_accounts_func=lambda: [{"id": 1, "name": "Checking"}, {"id": 2, "name": "Savings"}],
            list_envelopes_func=lambda **_: [
                {"id": 10, "locked_account_id": 1, "archived_at": None},
                {"id": 11, "locked_account_id": 1, "archived_at": None},
                {"id": 20, "locked_account_id": 2, "archived_at": None},
            ],
        )

        self.assertEqual(len(prefills["import_prefills"]), 1)
        prefill = prefills["import_prefills"][0]
        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transaction_type"], "transfer_out")
        self.assertEqual(prefill["transfer"]["other_account_id"], 2)
        self.assertEqual(prefill["transfer"]["other_account_name"], "Savings")
        self.assertEqual(prefill["transfer"]["current_account_splits"], [{"envelope_id": 10, "amount_cents": 4000}])
        self.assertEqual(prefill["transfer"]["current_account_remainder_envelope_id"], 11)
        self.assertEqual(prefill["transfer"]["current_account_remainder_amount_cents"], 6000)
        self.assertEqual(prefill["transfer"]["other_account_splits"], [{"envelope_id": 20, "amount_cents": 10000}])

    def test_transfer_action_withholds_invalid_transfer_payload_for_row(self) -> None:
        row = {"payee": "Online Transfer", "memo": "savings", "amount_cents": -9000}
        rule = self._rule(
            22,
            {"direction": "expense", "field": "memo", "operator": "contains", "value": "savings"},
            {
                "transfer": {
                    "transaction_type": "expense",
                    "other_account_id": 2,
                    "current_account_splits": [{"envelope_id": 10, "amount_cents": 4000}],
                    "current_account_remainder_envelope_id": 11,
                    "other_account_splits": [{"envelope_id": 20, "amount_cents": 10000}],
                    "target_amount_cents": -10000,
                },
            },
        )

        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
            list_accounts_func=lambda: [{"id": 1, "name": "Checking"}, {"id": 2, "name": "Savings"}],
            list_envelopes_func=lambda **_: [
                {"id": 10, "locked_account_id": 1, "archived_at": None},
                {"id": 11, "locked_account_id": 1, "archived_at": None},
                {"id": 20, "locked_account_id": 2, "archived_at": None},
            ],
        )

        self.assertEqual(len(prefills["import_prefills"]), 1)
        self.assertFalse(prefills["import_prefills"][0]["prefill"])
        self.assertIn("manual_rule_transfer_amount_mismatch", prefills["import_prefills"][0]["debug_reason_codes"])

    def test_parse_rule_form_accepts_manual_match_action_payload(self) -> None:
        data, errors = parse_rule_form(
            MultiDict({
                "name": "Manual match rule",
                "account_id": "1",
                "direction": "expense",
                "match_field": "payee",
                "match_operator": "contains",
                "match_value": "manual",
                "action_manual_match_json": json.dumps({"transaction_id": 42}),
            })
        )

        self.assertEqual(errors, [])
        self.assertEqual(data["action_json"]["manual_match"], {"transaction_id": 42})

    def test_manual_match_action_prefills_safe_proposal(self) -> None:
        row = {"posted_at": "2026-07-14", "payee": "Manual Coffee", "memo": "", "amount_cents": -1200}
        rule = self._rule(
            31,
            {"direction": "expense", "field": "payee", "operator": "contains", "value": "manual"},
            {"manual_match": {"transaction_id": 42}},
        )

        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
            get_transaction_func=lambda tx_id: {
                "id": tx_id,
                "account_id": 1,
                "posted_at": "2026-07-13",
                "amount_cents": -1200,
                "fitid": "",
            },
            existing_fitids=set(),
            row_states=[{"row_index": 0, "manual_match_eligible": True}],
        )

        self.assertEqual(len(prefills["import_prefills"]), 1)
        prefill = prefills["import_prefills"][0]
        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["manual_match"], {"transaction_id": 42})
        self.assertEqual(prefill["prediction_debug"]["decision"], "prefill")

    def test_manual_match_action_withholds_unsafe_targets(self) -> None:
        row = {"posted_at": "2026-07-14", "payee": "Manual Coffee", "memo": "", "amount_cents": -1200}
        rule = self._rule(
            32,
            {"direction": "expense", "field": "payee", "operator": "contains", "value": "manual"},
            {"manual_match": {"transaction_id": 42}},
        )

        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
            get_transaction_func=lambda _tx_id: {
                "id": 42,
                "account_id": 1,
                "posted_at": "2026-07-13",
                "amount_cents": -1200,
                "fitid": "already-imported",
            },
            existing_fitids={"already-imported"},
            row_states=[{"row_index": 0, "manual_match_eligible": True}],
        )

        self.assertEqual(len(prefills["import_prefills"]), 1)
        self.assertFalse(prefills["import_prefills"][0]["prefill"])
        self.assertIn("manual_rule_manual_match_duplicate_fitid", prefills["import_prefills"][0]["debug_reason_codes"])

    def test_manual_match_action_withholds_duplicate_import_row(self) -> None:
        row = {"posted_at": "2026-07-14", "payee": "Manual Coffee", "memo": "", "amount_cents": -1200}
        rule = self._rule(
            33,
            {"direction": "expense", "field": "payee", "operator": "contains", "value": "manual"},
            {"manual_match": {"transaction_id": 42}},
        )

        prefills = build_import_matching_rule_prefills(
            [row],
            1,
            list_rules_func=lambda **_: [rule],
            get_transaction_func=lambda _tx_id: {
                "id": 42,
                "account_id": 1,
                "posted_at": "2026-07-13",
                "amount_cents": -1200,
                "fitid": "",
            },
            row_states=[{"row_index": 0, "manual_match_eligible": False}],
        )

        self.assertFalse(prefills["import_prefills"][0]["prefill"])
        self.assertIn("manual_rule_manual_match_row_ineligible", prefills["import_prefills"][0]["debug_reason_codes"])

    def test_parse_rule_form_rejects_invalid_split_remainder_actions(self) -> None:
        base = {
            "name": "Invalid Split",
            "account_id": "1",
            "direction": "expense",
            "match_value": "bad",
        }
        envelope_rows = [
            {"id": 10, "name": "Archived", "locked_account_id": None, "archived_at": "2026-01-01"},
            {"id": 11, "name": "Other Account", "locked_account_id": 2, "archived_at": None},
            {"id": 12, "name": "Allowed", "locked_account_id": None, "archived_at": None},
        ]

        bad_cases = [
            ({"transaction_type": "expense", "splits": [{"envelope_id": 10, "amount_cents": -1000}]}, "unavailable envelope"),
            ({"transaction_type": "expense", "splits": [{"envelope_id": 11, "amount_cents": -1000}]}, "unavailable envelope"),
            ({"transaction_type": "expense", "splits": [{"envelope_id": 12, "amount_cents": 1000}]}, "incomplete or unsupported"),
            (
                {"transaction_type": "expense", "splits": [{"envelope_id": 12, "amount_cents": -1000}], "target_amount_cents": 2000},
                "incomplete or unsupported",
            ),
        ]
        for payload, expected in bad_cases:
            with self.subTest(expected=expected):
                data, errors = parse_rule_form(
                    MultiDict(dict(base, action_split_remainder_json=json.dumps(payload))),
                    list_envelopes_func=lambda **_: envelope_rows,
                )
                self.assertIsNone(data)
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_parse_rule_form_rejects_single_envelope_split_conflict(self) -> None:
        data, errors = parse_rule_form(
            MultiDict({
                "name": "Conflicting Split",
                "account_id": "1",
                "direction": "expense",
                "match_value": "bad",
                "action_envelope_id": "12",
                "action_split_remainder_json": json.dumps({
                    "transaction_type": "expense",
                    "splits": [{"envelope_id": 13, "amount_cents": -1000}],
                }),
            }),
            list_envelopes_func=lambda **_: [
                {"id": 12, "name": "Single", "locked_account_id": None, "archived_at": None},
                {"id": 13, "name": "Split", "locked_account_id": None, "archived_at": None},
            ],
        )

        self.assertIsNone(data)
        self.assertIn("Choose either a single-envelope action or a split/remainder action.", errors)


class ImportMatchingRuleStorageTests(FinanceAppTestCase):
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

    def test_split_remainder_rule_round_trips_through_storage_and_edit_post(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        envelope_a = envelopes_repo.get_envelope(envelopes_repo.insert_envelope({"name": "FIN091A Fixed"}))
        envelope_b = envelopes_repo.get_envelope(envelopes_repo.insert_envelope({"name": "FIN091A Remainder"}))
        split_payload = {
            "transaction_type": "income",
            "splits": [{"envelope_id": envelope_a["id"], "amount_cents": 5000, "amount_mode": "signed"}],
            "remainder_envelope_id": envelope_b["id"],
            "target_amount_cents": 9000,
        }
        data, errors = parse_rule_form(
            MultiDict({
                "name": "FIN091A Round Trip",
                "account_scope": "account",
                "account_id": str(account["id"]),
                "enabled": "1",
                "priority": "80",
                "direction": "income",
                "match_field": "payee",
                "match_operator": "contains",
                "match_value": "bonus",
                "action_split_remainder_json": json.dumps(split_payload),
            })
        )
        self.assertEqual(errors, [])

        rule_id = import_matching_rules_repo.create_import_matching_rule(data)
        stored = import_matching_rules_repo.get_import_matching_rule(rule_id)
        self.assertEqual(stored["action_json"]["split_remainder"]["remainder_envelope_id"], envelope_b["id"])

        edit = self.client.get(f"/imports/rules/{rule_id}/edit")
        self.assertEqual(edit.status_code, 200)
        self.assertIn('name="action_split_remainder_json"', edit.get_data(as_text=True))

        updated = self.client.post(
            f"/imports/rules/{rule_id}/edit",
            data={
                "name": "FIN091A Round Trip Updated",
                "account_scope": "account",
                "account_id": str(account["id"]),
                "enabled": "1",
                "priority": "70",
                "direction": "income",
                "match_field": "payee",
                "match_operator": "contains",
                "match_value": "bonus",
                "action_split_remainder_json": json.dumps(stored["action_json"]["split_remainder"]),
            },
        )
        self.assertEqual(updated.status_code, 302)
        after = import_matching_rules_repo.get_import_matching_rule(rule_id)
        self.assertEqual(after["priority"], 70)
        self.assertEqual(after["action_json"]["split_remainder"], stored["action_json"]["split_remainder"])

    def test_transfer_rule_round_trips_through_storage_and_edit_post(self) -> None:
        self._select_user_in_client()
        account_a, account_b = accounts_repo.list_accounts()[:2]
        envelope_a = envelopes_repo.get_envelope(envelopes_repo.insert_envelope({
            "name": "FIN091C Current",
            "locked_account_id": account_a["id"],
        }))
        envelope_b = envelopes_repo.get_envelope(envelopes_repo.insert_envelope({
            "name": "FIN091C Other",
            "locked_account_id": account_b["id"],
        }))
        transfer_payload = {
            "transaction_type": "expense",
            "other_account_id": account_b["id"],
            "current_account_splits": [{"envelope_id": envelope_a["id"], "amount_cents": 5000}],
            "other_account_splits": [{"envelope_id": envelope_b["id"], "amount_cents": 5000}],
            "target_amount_cents": 5000,
        }
        data, errors = parse_rule_form(
            MultiDict({
                "name": "FIN091C Transfer",
                "account_scope": "account",
                "account_id": str(account_a["id"]),
                "enabled": "1",
                "priority": "80",
                "direction": "expense",
                "match_field": "memo",
                "match_operator": "contains",
                "match_value": "transfer",
                "action_transfer_json": json.dumps(transfer_payload),
            })
        )
        self.assertEqual(errors, [])

        rule_id = import_matching_rules_repo.create_import_matching_rule(data)
        stored = import_matching_rules_repo.get_import_matching_rule(rule_id)
        self.assertEqual(stored["action_json"]["transfer"]["other_account_id"], account_b["id"])

        updated = self.client.post(
            f"/imports/rules/{rule_id}/edit",
            data={
                "name": "FIN091C Transfer Updated",
                "account_scope": "account",
                "account_id": str(account_a["id"]),
                "enabled": "1",
                "priority": "70",
                "direction": "expense",
                "match_field": "memo",
                "match_operator": "contains",
                "match_value": "transfer",
                "action_transfer_json": json.dumps(stored["action_json"]["transfer"]),
            },
        )
        self.assertEqual(updated.status_code, 302)
        after = import_matching_rules_repo.get_import_matching_rule(rule_id)
        self.assertEqual(after["priority"], 70)
        self.assertEqual(after["action_json"]["transfer"], stored["action_json"]["transfer"])

    def test_manual_match_rule_round_trips_through_storage_and_edit_post(self) -> None:
        self._select_user_in_client()
        account = accounts_repo.list_accounts()[0]
        data, errors = parse_rule_form(
            MultiDict({
                "name": "FIN091D Manual Match",
                "account_scope": "account",
                "account_id": str(account["id"]),
                "enabled": "1",
                "priority": "80",
                "direction": "expense",
                "match_field": "payee",
                "match_operator": "contains",
                "match_value": "manual",
                "action_manual_match_json": json.dumps({"transaction_id": 42}),
            })
        )
        self.assertEqual(errors, [])

        rule_id = import_matching_rules_repo.create_import_matching_rule(data)
        stored = import_matching_rules_repo.get_import_matching_rule(rule_id)
        self.assertEqual(stored["action_json"]["manual_match"], {"transaction_id": 42})

        updated = self.client.post(
            f"/imports/rules/{rule_id}/edit",
            data={
                "name": "FIN091D Manual Match Updated",
                "account_scope": "account",
                "account_id": str(account["id"]),
                "enabled": "1",
                "priority": "70",
                "direction": "expense",
                "match_field": "payee",
                "match_operator": "contains",
                "match_value": "manual",
                "action_manual_match_json": json.dumps(stored["action_json"]["manual_match"]),
            },
        )
        self.assertEqual(updated.status_code, 302)
        after = import_matching_rules_repo.get_import_matching_rule(rule_id)
        self.assertEqual(after["priority"], 70)
        self.assertEqual(after["action_json"]["manual_match"], stored["action_json"]["manual_match"])


if __name__ == "__main__":
    unittest.main()
