import json
import unittest

from app.db import get_db
from app.services.import_matching_rule_service import build_import_matching_rule_prefills
from app.services.import_rule_proposal_service import build_import_rule_proposals
from tests.helpers import FinanceAppTestCase


def _learning_example(
    idx: int,
    *,
    raw_payee: str,
    raw_memo: str = "",
    final_payee: str = "Joe's Coffee",
    transaction_type: str = "expense",
    envelope_id: int | None = 9,
    amount_cents: int | None = None,
    splits: list[dict] | None = None,
    remainder_intent: dict | None = None,
    source: str = "import_commit",
    transaction_id: int | None = None,
    paired_account_id: int | None = None,
    paired_transaction: dict | None = None,
    decision: dict | None = None,
    fitid: str | None = None,
    fingerprint: str | None = None,
    feedback: dict | None = None,
) -> dict:
    final_amount_cents = amount_cents if amount_cents is not None else -500 - idx
    return {
        "learning_example_id": idx,
        "account_id": 1,
        "transaction_id": transaction_id if transaction_id is not None else 1000 + idx,
        "source": source,
        "evidence_quality": "high",
        "posted_at": f"2026-07-{idx:02d}",
        "amount_cents": final_amount_cents,
        "raw_payee": raw_payee,
        "raw_memo": raw_memo,
        "final_payee": final_payee,
        "final_memo": raw_memo,
        "ttype": transaction_type,
        "splits": splits if splits is not None else ([{"envelope_id": envelope_id, "amount_cents": final_amount_cents}] if envelope_id else []),
        "remainder_intent": remainder_intent,
        "paired_account_id": paired_account_id,
        "paired_transaction": paired_transaction,
        "decision": decision or {},
        "learning_evidence": {
            "import_row": {
                "fitid": fitid or f"fit-{idx}",
                "row_fingerprint": fingerprint or f"fp-{idx}",
            },
        },
        "prediction_feedback": feedback or {"accepted": 1, "modified": 0, "rejected": 0},
    }


class ImportRuleProposalServiceUnitTests(unittest.TestCase):
    def test_suggests_conservative_simple_rule_with_distinct_raw_keys(self) -> None:
        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: [
                _learning_example(1, raw_payee="SQ *JOES COFFEE 1001", raw_memo="CARD 11"),
                _learning_example(2, raw_payee="SQ *JOES COFFEE 1002", raw_memo="CARD 22"),
                _learning_example(3, raw_payee="SQ *JOES COFFEE 1003", raw_memo="CARD 33"),
            ],
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
        )

        self.assertEqual(len(result["proposals"]), 1)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["decision"], "suggest")
        self.assertEqual(proposal["reason_codes"], ["rule_proposal_conservative_support_met"])
        self.assertEqual(proposal["condition_json"], {
            "direction": "expense",
            "field": "text",
            "operator": "contains",
            "value": "joes coffee",
        })
        self.assertEqual(proposal["action_json"], {
            "payee": "Joe's Coffee",
            "transaction_type": "expense",
            "single_envelope_id": 9,
        })
        self.assertEqual(proposal["suggested_rule"]["enabled"], False)
        self.assertEqual(proposal["evidence"]["support_examples"], 3)
        self.assertEqual(proposal["evidence"]["distinct_raw_identities"], 3)
        self.assertEqual(proposal["evidence"]["feedback_accepted"], 3)

    def test_withholds_duplicate_identical_bankfeed_rows(self) -> None:
        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: [
                _learning_example(1, raw_payee="JOES COFFEE SHOP 1001", fitid="same-fit", fingerprint="same-fp"),
                _learning_example(2, raw_payee="JOES COFFEE SHOP 1001", fitid="same-fit", fingerprint="same-fp"),
                _learning_example(3, raw_payee="JOES COFFEE SHOP 1001", fitid="same-fit", fingerprint="same-fp"),
            ],
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
        )

        self.assertEqual(result["proposals"], [])
        self.assertEqual(len(result["withheld"]), 1)
        withheld = result["withheld"][0]
        self.assertIn("rule_proposal_insufficient_support", withheld["reason_codes"])
        self.assertIn("rule_proposal_insufficient_distinct_raw_identities", withheld["reason_codes"])
        self.assertEqual(withheld["evidence"]["support_examples"], 1)
        self.assertEqual(withheld["evidence"]["distinct_raw_identities"], 1)

    def test_withholds_weak_processor_predicate_before_overreach(self) -> None:
        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: [
                _learning_example(1, raw_payee="SQ *1234", raw_memo="CARD 11"),
                _learning_example(2, raw_payee="SQ *5678", raw_memo="CARD 22"),
                _learning_example(3, raw_payee="SQ *9012", raw_memo="CARD 33"),
            ],
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
        )

        self.assertEqual(result["proposals"], [])
        self.assertGreaterEqual(len(result["withheld"]), 3)
        self.assertTrue(all("rule_proposal_predicate_too_broad" in item["reason_codes"] for item in result["withheld"]))

    def test_withholds_ambiguous_cluster_actions(self) -> None:
        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: [
                _learning_example(1, raw_payee="JOES COFFEE SHOP 1001", final_payee="Joe's Coffee", envelope_id=9),
                _learning_example(2, raw_payee="JOES COFFEE SHOP 1002", final_payee="Joes Cafe", envelope_id=9),
                _learning_example(3, raw_payee="JOES COFFEE SHOP 1003", final_payee="Joe's Coffee", envelope_id=10),
            ],
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
        )

        self.assertEqual(result["proposals"], [])
        withheld = result["withheld"][0]
        self.assertIn("rule_proposal_ambiguous_payee", withheld["reason_codes"])
        self.assertIn("rule_proposal_ambiguous_single_envelope", withheld["reason_codes"])
        self.assertIn("unsupported_sources", withheld["evidence"])

    def test_payee_cleanup_history_can_support_payee_only_candidate(self) -> None:
        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: [],
            list_payee_rules_func=lambda **_: [
                {
                    "id": 1,
                    "raw_payee_key": "joes coffee 1001",
                    "raw_memo_key": "",
                    "raw_payee_sample": "JOES COFFEE 1001",
                    "canonical_payee": "Joe's Coffee",
                    "payee_changed": 1,
                    "use_count": 2,
                },
                {
                    "id": 2,
                    "raw_payee_key": "joes coffee 1002",
                    "raw_memo_key": "",
                    "raw_payee_sample": "JOES COFFEE 1002",
                    "canonical_payee": "Joe's Coffee",
                    "payee_changed": 1,
                    "use_count": 1,
                },
            ],
            list_existing_rules_func=lambda **_: [],
        )

        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(result["proposals"][0]["action_json"], {"payee": "Joe's Coffee"})
        self.assertEqual(result["source_notes"][1]["source"], "payee_normalization_rules")

    def test_suggests_conservative_split_remainder_action_with_repeated_advanced_evidence(self) -> None:
        examples = [
            _learning_example(
                idx,
                raw_payee=f"ACME UTILITY COMPANY PORTLAND {1000 + idx}",
                final_payee="Fin092D Utility",
                envelope_id=None,
                amount_cents=-10000 - (idx * 1000),
                splits=[
                    {"envelope_id": 10, "amount_cents": -2500},
                    {"envelope_id": 11, "amount_cents": -7500 - (idx * 1000)},
                ],
                remainder_intent={"envelope_id": 11, "amount_cents": -7500 - (idx * 1000)},
            )
            for idx in range(1, 5)
        ]

        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: examples,
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
            list_envelopes_func=lambda **_: [
                {"id": 10, "name": "Fixed", "locked_account_id": None, "archived_at": None},
                {"id": 11, "name": "Remainder", "locked_account_id": 1, "archived_at": None},
            ],
        )

        self.assertEqual(len(result["proposals"]), 1)
        action = result["proposals"][0]["action_json"]
        self.assertEqual(action["split_remainder"], {
            "transaction_type": "expense",
            "splits": [{"envelope_id": 10, "amount_cents": -2500, "amount_mode": "signed"}],
            "remainder_envelope_id": 11,
        })
        self.assertEqual(result["proposals"][0]["evidence"]["support_examples"], 4)
        self.assertEqual(result["proposals"][0]["reason_codes"], ["rule_proposal_conservative_support_met"])

    def test_withholds_advanced_split_evidence_when_partially_supported(self) -> None:
        examples = [
            _learning_example(
                1,
                raw_payee="ACME PARTIAL UTILITY PORTLAND 1001",
                envelope_id=None,
                amount_cents=-10000,
                splits=[{"envelope_id": 10, "amount_cents": -2500}, {"envelope_id": 11, "amount_cents": -7500}],
                remainder_intent={"envelope_id": 11, "amount_cents": -7500},
            ),
            _learning_example(2, raw_payee="ACME PARTIAL UTILITY PORTLAND 1002", envelope_id=10, amount_cents=-10000),
            _learning_example(3, raw_payee="ACME PARTIAL UTILITY PORTLAND 1003", envelope_id=10, amount_cents=-10000),
            _learning_example(4, raw_payee="ACME PARTIAL UTILITY PORTLAND 1004", envelope_id=10, amount_cents=-10000),
        ]

        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: examples,
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
            list_envelopes_func=lambda **_: [
                {"id": 10, "name": "Fixed", "locked_account_id": None, "archived_at": None},
                {"id": 11, "name": "Remainder", "locked_account_id": None, "archived_at": None},
            ],
        )

        self.assertEqual(result["proposals"], [])
        self.assertIn("rule_proposal_advanced_partial_evidence", result["withheld"][0]["reason_codes"])
        self.assertNotIn("single_envelope_id", result["withheld"][0]["action_json"])

    def test_suggests_transfer_action_with_paired_transfer_provenance_and_exact_amount(self) -> None:
        examples = [
            _learning_example(
                idx,
                raw_payee=f"ACME SAVINGS TRANSFER PORTLAND {1000 + idx}",
                final_payee="Fin092D Savings",
                transaction_type="transfer_out",
                envelope_id=None,
                amount_cents=-15000,
                splits=[{"envelope_id": 10, "amount_cents": -5000}, {"envelope_id": 11, "amount_cents": -10000}],
                remainder_intent={"envelope_id": 11, "amount_cents": -10000},
                paired_account_id=2,
                paired_transaction={
                    "id": 4000 + idx,
                    "account_id": 2,
                    "ttype": "transfer_in",
                    "amount_cents": 15000,
                    "splits": [{"envelope_id": 20, "amount_cents": 15000}],
                    "remainder_intent": None,
                },
            )
            for idx in range(1, 5)
        ]

        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: examples,
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
            list_accounts_func=lambda: [{"id": 1, "name": "Checking"}, {"id": 2, "name": "Savings"}],
            list_envelopes_func=lambda **_: [
                {"id": 10, "locked_account_id": 1, "archived_at": None},
                {"id": 11, "locked_account_id": None, "archived_at": None},
                {"id": 20, "locked_account_id": 2, "archived_at": None},
            ],
        )

        self.assertEqual(len(result["proposals"]), 1)
        transfer = result["proposals"][0]["action_json"]["transfer"]
        self.assertEqual(transfer["transaction_type"], "expense")
        self.assertEqual(transfer["other_account_id"], 2)
        self.assertEqual(transfer["current_account_splits"], [{"envelope_id": 10, "amount_cents": 5000}])
        self.assertEqual(transfer["current_account_remainder_envelope_id"], 11)
        self.assertEqual(transfer["other_account_splits"], [{"envelope_id": 20, "amount_cents": 15000}])
        self.assertEqual(transfer["target_amount_cents"], -15000)

    def test_withholds_transfer_without_distinct_provenance_or_pair_safety(self) -> None:
        examples = [
            _learning_example(
                idx,
                raw_payee=f"ACME UNSAFE TRANSFER PORTLAND {1000 + idx}",
                transaction_type="transfer_out",
                envelope_id=None,
                amount_cents=-15000,
                splits=[{"envelope_id": 10, "amount_cents": -15000}],
                paired_account_id=2,
                paired_transaction={"id": 5000 + idx, "account_id": 2, "ttype": "transfer_in", "amount_cents": 14999, "splits": [{"envelope_id": 20, "amount_cents": 14999}]},
                fitid="same-fitid",
                fingerprint="same-fingerprint",
            )
            for idx in range(1, 5)
        ]

        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: examples,
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
            list_accounts_func=lambda: [{"id": 1, "name": "Checking"}, {"id": 2, "name": "Savings"}],
            list_envelopes_func=lambda **_: [
                {"id": 10, "locked_account_id": 1, "archived_at": None},
                {"id": 20, "locked_account_id": 2, "archived_at": None},
            ],
        )

        self.assertEqual(result["proposals"], [])
        withheld = result["withheld"][0]
        self.assertIn("rule_proposal_transfer_pair_amount_mismatch", withheld["reason_codes"])
        self.assertIn("rule_proposal_insufficient_support", withheld["reason_codes"])

    def test_suggests_manual_match_only_with_repeated_distinct_provenance(self) -> None:
        examples = [
            _learning_example(
                idx,
                raw_payee=f"ACME MANUAL MATCH PORTLAND {1000 + idx}",
                source="manual_match",
                transaction_id=4242,
                envelope_id=9,
                amount_cents=-1200,
                decision={"source_action": "manual_match"},
            )
            for idx in range(1, 5)
        ]

        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: examples,
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
        )

        self.assertEqual(len(result["proposals"]), 1)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["action_json"]["manual_match"], {"transaction_id": 4242})
        self.assertEqual(proposal["condition_json"]["amount_min_cents"], 1200)
        self.assertEqual(proposal["condition_json"]["amount_max_cents"], 1200)

    def test_withholds_manual_match_duplicate_provenance(self) -> None:
        examples = [
            _learning_example(
                idx,
                raw_payee=f"ACME MANUAL DUPLICATE PORTLAND {1000 + idx}",
                source="manual_match",
                transaction_id=4242,
                envelope_id=9,
                amount_cents=-1200,
                fitid="same-fitid",
                fingerprint="same-fingerprint",
                decision={"source_action": "manual_match"},
            )
            for idx in range(1, 5)
        ]

        result = build_import_rule_proposals(
            account_id=1,
            list_learning_examples_func=lambda **_: examples,
            list_payee_rules_func=lambda **_: [],
            list_existing_rules_func=lambda **_: [],
        )

        self.assertEqual(result["proposals"], [])
        withheld = result["withheld"][0]
        self.assertIn("rule_proposal_insufficient_support", withheld["reason_codes"])
        self.assertIn("rule_proposal_manual_match_insufficient_distinct_provenance", withheld["reason_codes"])


class ImportRuleProposalSideEffectTests(FinanceAppTestCase):
    def test_backend_proposal_engine_does_not_create_or_apply_rules(self) -> None:
        db = get_db()
        before_rules = db.execute("SELECT COUNT(1) FROM import_matching_rules").fetchone()[0]
        for idx, raw_suffix in enumerate(("1001", "1002", "1003"), start=1):
            db.execute(
                """
                INSERT INTO transaction_learning_examples(
                    account_id, transaction_id, source, evidence_quality, dedupe_key,
                    posted_at, amount_cents, raw_payee, raw_memo,
                    final_payee, final_memo, transaction_type, splits_json,
                    remainder_intent_json, decision_json, evidence_json, created_at, updated_at
                ) VALUES (
                    1, NULL, 'import_commit', 'high', ?,
                    ?, -625, ?, 'CARD',
                    'Fin092A Roastery', 'CARD', 'expense', ?,
                    '{}', '{}', ?, '2026-07-15T10:00:00', '2026-07-15T10:00:00'
                )
                """,
                (
                    f"fin092a:{idx}",
                    f"2026-07-{idx:02d}",
                    f"FINTEST ROASTERY {raw_suffix}",
                    json.dumps([{"envelope_id": 1, "amount_cents": -625}]),
                    json.dumps({"import_row": {"fitid": f"fin092a-fit-{idx}", "row_fingerprint": f"fin092a-fp-{idx}"}}),
                ),
            )
        db.commit()

        result = build_import_rule_proposals(account_id=1)

        after_rules = db.execute("SELECT COUNT(1) FROM import_matching_rules").fetchone()[0]
        self.assertEqual(after_rules, before_rules)
        self.assertTrue(any(
            proposal["condition_json"]["value"] == "fintest roastery"
            for proposal in result["proposals"]
        ))
        prefills = build_import_matching_rule_prefills(
            [{"payee": "FINTEST ROASTERY 9999", "memo": "CARD", "amount_cents": -625}],
            1,
        )
        self.assertEqual(prefills, {"import_prefills": [], "payee_prefills": []})


if __name__ == "__main__":
    unittest.main()
