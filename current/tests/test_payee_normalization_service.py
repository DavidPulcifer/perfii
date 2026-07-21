import sqlite3

from app.db import get_db, table_exists
from app.repositories.payee_normalization_repo import (
    list_payee_normalization_rules,
    record_payee_normalization_example,
)
from app.services.payee_normalization_service import (
    build_payee_normalization_prefills,
    cleanup_part_differs_meaningfully,
    import_identity_keys,
    list_cleanup_learning_examples,
    normalize_import_identity_part,
    payee_normalization_example_from_import_row,
    payee_differs_meaningfully,
)
from tests.helpers import FinanceAppTestCase


class PayeeNormalizationServiceTests(FinanceAppTestCase):
    def test_import_identity_key_is_conservative_and_keeps_digits(self) -> None:
        self.assertEqual(
            normalize_import_identity_part(" SQ *Coffee-1234  "),
            "sq coffee 1234",
        )
        self.assertEqual(import_identity_keys("PAYEE #1234", "Memo/9988"), ("payee 1234", "memo 9988"))

    def test_payee_differs_meaningfully_ignores_punctuation_only_changes(self) -> None:
        self.assertFalse(payee_differs_meaningfully("Coffee-Shop", "coffee shop"))
        self.assertTrue(payee_differs_meaningfully("SQ *COFFEE 1234", "Coffee Shop"))
        self.assertTrue(cleanup_part_differs_meaningfully("CARD 0001", "Latte"))
        self.assertTrue(cleanup_part_differs_meaningfully("raw memo", ""))

    def test_build_payee_prefills_uses_exact_raw_payee_and_memo_key(self) -> None:
        rows = [
            {"payee": "SQ *COFFEE 1234", "memo": "CARD 0001"},
            {"payee": "SQ *COFFEE 5678", "memo": "CARD 0001"},
        ]
        seen = []

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: seen.append(kwargs) or [
                {
                    "id": 44,
                    "raw_payee_key": "sq coffee 1234",
                    "raw_memo_key": "card 0001",
                    "canonical_payee": "Coffee Shop",
                    "canonical_memo": "Latte",
                    "payee_changed": 1,
                    "memo_changed": 1,
                }
            ],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertEqual(seen[0]["account_id"], 7)
        self.assertTrue(prefills[0]["payee_prefill"])
        self.assertTrue(prefills[0]["memo_prefill"])
        self.assertEqual(prefills[0]["row_index"], 0)
        self.assertEqual(prefills[0]["canonical_payee"], "Coffee Shop")
        self.assertEqual(prefills[0]["canonical_memo"], "Latte")
        self.assertEqual(prefills[0]["rule_id"], 44)
        self.assertEqual(prefills[0]["debug_reason_codes"], ["matched_raw_text_profile"])
        self.assertEqual(prefills[0]["prediction_debug"]["engine"], "payee_cleanup")
        self.assertEqual(prefills[0]["prediction_debug"]["prediction_type"], "payee_memo_cleanup")
        self.assertEqual(prefills[1], {"row_index": 1, "payee_prefill": False})

    def test_build_payee_prefills_uses_strong_profile_rule_without_exact_digits(self) -> None:
        rows = [{"payee": "SQ *JOES COFFEE 000999", "memo": "CARD 2222"}]

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: [] if kwargs.get("keys") else [
                {
                    "id": 45,
                    "raw_payee_key": "sq joes coffee 000123",
                    "raw_memo_key": "card 1111",
                    "raw_payee_sample": "SQ *JOES COFFEE 000123",
                    "raw_memo_sample": "CARD 1111",
                    "canonical_payee": "Joe's Coffee",
                    "canonical_memo": "Morning coffee",
                    "payee_changed": 1,
                    "memo_changed": 1,
                    "use_count": 1,
                },
                {
                    "id": 46,
                    "raw_payee_key": "sq joes coffee 000456",
                    "raw_memo_key": "card 3333",
                    "raw_payee_sample": "SQ *JOES COFFEE 000456",
                    "raw_memo_sample": "CARD 3333",
                    "canonical_payee": "Joe's Coffee",
                    "canonical_memo": "Morning coffee",
                    "payee_changed": 1,
                    "memo_changed": 1,
                    "use_count": 1,
                }
            ],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertTrue(prefills[0]["payee_prefill"])
        self.assertTrue(prefills[0]["memo_prefill"])
        self.assertEqual(prefills[0]["canonical_payee"], "Joe's Coffee")
        self.assertEqual(prefills[0]["canonical_memo"], "Morning coffee")
        self.assertIn("payee_learned_from_profile_cluster", prefills[0]["debug_reason_codes"])
        self.assertEqual(prefills[0]["prediction_debug"]["evidence"]["support_count"], 2)

    def test_build_payee_prefills_uses_amazon_profile_cluster_for_new_suffix(self) -> None:
        rows = [{"payee": "AMAZON MKTPL*XYZ9876", "memo": ""}]

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: [] if kwargs.get("keys") else [
                {
                    "id": 45,
                    "raw_payee_key": "amazon mktpl demo00001",
                    "raw_memo_key": "",
                    "raw_payee_sample": "AMAZON MKTPL*DEMO00001",
                    "canonical_payee": "AMAZON",
                    "payee_changed": 1,
                    "use_count": 1,
                },
                {
                    "id": 46,
                    "raw_payee_key": "amazon mktpl demo00002",
                    "raw_memo_key": "",
                    "raw_payee_sample": "AMAZON MKTPL*DEMO00002",
                    "canonical_payee": "AMAZON",
                    "payee_changed": 1,
                    "use_count": 1,
                },
            ],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertTrue(prefills[0]["payee_prefill"])
        self.assertEqual(prefills[0]["canonical_payee"], "AMAZON")
        self.assertEqual(prefills[0]["debug_reason_codes"], ["payee_learned_from_profile_cluster"])
        evidence = prefills[0]["prediction_debug"]["evidence"]
        self.assertEqual(evidence["cluster_signature"], "amazon mktpl")
        self.assertEqual(evidence["support_count"], 2)

    def test_build_payee_prefills_exact_rule_wins_over_profile_cluster(self) -> None:
        rows = [{"payee": "AMAZON MKTPL*XYZ9876", "memo": ""}]

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: [
                {
                    "id": 50,
                    "raw_payee_key": "amazon mktpl xyz9876",
                    "raw_memo_key": "",
                    "canonical_payee": "Exact Amazon",
                    "payee_changed": 1,
                }
            ] if kwargs.get("keys") else [
                {
                    "id": 45,
                    "raw_payee_key": "amazon mktpl demo00001",
                    "raw_payee_sample": "AMAZON MKTPL*DEMO00001",
                    "canonical_payee": "Cluster Amazon",
                    "payee_changed": 1,
                    "use_count": 1,
                },
                {
                    "id": 46,
                    "raw_payee_key": "amazon mktpl demo00002",
                    "raw_payee_sample": "AMAZON MKTPL*DEMO00002",
                    "canonical_payee": "Cluster Amazon",
                    "payee_changed": 1,
                    "use_count": 1,
                },
            ],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertEqual(prefills[0]["canonical_payee"], "Exact Amazon")
        self.assertEqual(prefills[0]["debug_reason_codes"], ["matched_raw_text_profile"])

    def test_build_payee_prefills_requires_distinct_raw_keys_for_profile_cluster(self) -> None:
        rows = [{"payee": "AMAZON MKTPL*XYZ9876", "memo": ""}]

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: [] if kwargs.get("keys") else [
                {
                    "id": 45,
                    "raw_payee_key": "amazon mktpl demo00001",
                    "raw_payee_sample": "AMAZON MKTPL*DEMO00001",
                    "canonical_payee": "AMAZON",
                    "payee_changed": 1,
                    "use_count": 5,
                },
            ],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertFalse(prefills[0]["payee_prefill"])
        self.assertEqual(prefills[0]["debug_reason_codes"], ["profile_cluster_insufficient_support"])

    def test_build_payee_prefills_skips_ambiguous_profile_rules(self) -> None:
        rows = [{"payee": "SQ *JOES COFFEE 000999", "memo": "CARD 2222"}]

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: [] if kwargs.get("keys") else [
                {
                    "id": 45,
                    "raw_payee_sample": "SQ *JOES COFFEE 000123",
                    "canonical_payee": "Joe's Coffee",
                    "use_count": 3,
                },
                {
                    "id": 46,
                    "raw_payee_sample": "SQ JOES COFFEE 000456",
                    "canonical_payee": "Joes Cafe",
                    "use_count": 3,
                },
            ],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertFalse(prefills[0]["payee_prefill"])
        self.assertEqual(prefills[0]["debug_reason_codes"], ["profile_cluster_ambiguous"])

    def test_build_payee_prefills_keeps_amazon_variants_separate(self) -> None:
        rows = [{"payee": "AMAZON PRIME*XYZ", "memo": ""}]

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: [] if kwargs.get("keys") else [
                {
                    "id": 45,
                    "raw_payee_key": "amazon mktpl demo00001",
                    "raw_payee_sample": "AMAZON MKTPL*DEMO00001",
                    "canonical_payee": "AMAZON",
                    "payee_changed": 1,
                    "use_count": 1,
                },
                {
                    "id": 46,
                    "raw_payee_key": "amazon mktpl demo00002",
                    "raw_payee_sample": "AMAZON MKTPL*DEMO00002",
                    "canonical_payee": "AMAZON",
                    "payee_changed": 1,
                    "use_count": 1,
                },
            ],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertEqual(prefills[0], {"row_index": 0, "payee_prefill": False})

    def test_build_payee_prefills_skips_weak_profile_clusters(self) -> None:
        rows = [{"payee": "SP Vendor 999", "memo": ""}]

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: [] if kwargs.get("keys") else [
                {
                    "id": 45,
                    "raw_payee_key": "sp vendor 123",
                    "raw_payee_sample": "SP Vendor 123",
                    "canonical_payee": "Vendor",
                    "payee_changed": 1,
                    "use_count": 4,
                },
            ],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertEqual(prefills[0], {"row_index": 0, "payee_prefill": False})

    def test_build_payee_prefills_can_learn_memo_without_payee_change(self) -> None:
        rows = [{"payee": "TARGET STORE 1234", "memo": "CARD 0001"}]

        prefills = build_payee_normalization_prefills(
            rows,
            7,
            list_rules_func=lambda **kwargs: [
                {
                    "id": 47,
                    "raw_payee_key": "target store 1234",
                    "raw_memo_key": "card 0001",
                    "canonical_payee": "TARGET STORE 1234",
                    "canonical_memo": "Groceries",
                    "payee_changed": 0,
                    "memo_changed": 1,
                    "use_count": 2,
                }
            ] if kwargs.get("keys") else [],
            list_learning_examples_func=lambda **kwargs: [],
        )

        self.assertFalse(prefills[0]["payee_prefill"])
        self.assertTrue(prefills[0]["memo_prefill"])
        self.assertNotIn("canonical_payee", prefills[0])
        self.assertEqual(prefills[0]["canonical_memo"], "Groceries")

    def test_learning_examples_provide_transfer_display_payee_and_memo(self) -> None:
        db = get_db()
        now = "2026-06-21T12:00:00+00:00"
        db.execute(
            """
            INSERT INTO transaction_learning_examples(
                account_id, transaction_id, source, evidence_quality, dedupe_key,
                posted_at, amount_cents, raw_payee, raw_memo, raw_profile_json,
                final_payee, final_memo, final_profile_json, transaction_type,
                transfer_other_account_id, splits_json, remainder_intent_json,
                decision_json, evidence_json, created_at, updated_at
            ) VALUES (
                1, NULL, 'backfill', 'high', 'test-transfer-cleanup',
                '2026-06-20', -12500, 'Online Transfer',
                'to SAV ...0101 trace 000777', '{}',
                'Example Bank - 0101', 'Savings transfer', '{}', 'transfer_out',
                NULL, '[]', '{}', '{}', '{}', ?, ?
            )
            """,
            (now, now),
        )
        db.commit()

        prefills = build_payee_normalization_prefills(
            [{"payee": "Online Transfer", "memo": "to SAV ...0101 trace 000777"}],
            1,
        )

        self.assertTrue(prefills[0]["payee_prefill"])
        self.assertTrue(prefills[0]["memo_prefill"])
        self.assertEqual(prefills[0]["canonical_payee"], "Example Bank - 0101")
        self.assertEqual(prefills[0]["canonical_memo"], "Savings transfer")

    def test_learning_examples_skip_ambiguous_exact_cleanup(self) -> None:
        db = get_db()
        now = "2026-06-21T12:00:00+00:00"
        for idx, payee in enumerate(("Coffee Shop", "Coffee Kiosk"), start=1):
            db.execute(
                """
                INSERT INTO transaction_learning_examples(
                    account_id, source, evidence_quality, dedupe_key, raw_payee,
                    raw_memo, raw_profile_json, final_payee, final_memo,
                    final_profile_json, splits_json, remainder_intent_json,
                    decision_json, evidence_json, created_at, updated_at
                ) VALUES (
                    1, 'backfill', 'high', ?, 'SQ *COFFEE 1234', 'CARD 0001',
                    '{}', ?, 'Latte', '{}', '[]', '{}', '{}', '{}', ?, ?
                )
                """,
                (f"test-ambiguous-{idx}", payee, now, now),
            )
        db.commit()

        prefills = build_payee_normalization_prefills(
            [{"payee": "SQ *COFFEE 1234", "memo": "CARD 0001"}],
            1,
        )

        self.assertEqual(prefills[0], {"row_index": 0, "payee_prefill": False})

    def test_import_row_example_records_payee_and_memo_before_after(self) -> None:
        class Row:
            orig_payee = "SQ *COFFEE 1234"
            payee = "Coffee Shop"
            orig_memo = "CARD 0001"
            memo = "Latte"

        example = payee_normalization_example_from_import_row(Row(), account_id=3)

        self.assertEqual(example["canonical_payee"], "Coffee Shop")
        self.assertEqual(example["canonical_memo"], "Latte")
        self.assertTrue(example["payee_changed"])
        self.assertTrue(example["memo_changed"])

    def test_repo_records_and_updates_account_scoped_rule(self) -> None:
        db = get_db()
        self.assertTrue(table_exists(db, "payee_normalization_rules"))

        record_payee_normalization_example(
            account_id=1,
            raw_payee_key="sq coffee 1234",
            raw_memo_key="card 0001",
            raw_payee_sample="SQ *COFFEE 1234",
            raw_memo_sample="CARD 0001",
            canonical_payee="Coffee Shop",
            canonical_memo="Latte",
            payee_changed=True,
            memo_changed=True,
        )
        record_payee_normalization_example(
            account_id=1,
            raw_payee_key="sq coffee 1234",
            raw_memo_key="card 0001",
            raw_payee_sample="SQ *COFFEE 1234",
            raw_memo_sample="CARD 0001",
            canonical_payee="Coffee Shop Main",
            canonical_memo="Coffee",
            payee_changed=True,
            memo_changed=True,
        )
        db.commit()

        rules = list_payee_normalization_rules(
            account_id=1,
            keys=[("sq coffee 1234", "card 0001")],
        )

        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["canonical_payee"], "Coffee Shop Main")
        self.assertEqual(rules[0]["canonical_memo"], "Coffee")
        self.assertEqual(rules[0]["payee_changed"], 1)
        self.assertEqual(rules[0]["memo_changed"], 1)
        self.assertEqual(rules[0]["use_count"], 2)

    def test_repo_lists_strong_account_scoped_rules_for_profile_learning(self) -> None:
        record_payee_normalization_example(
            account_id=1,
            raw_payee_key="sq joes coffee 000123",
            raw_memo_key="card 1111",
            raw_payee_sample="SQ *JOES COFFEE 000123",
            raw_memo_sample="CARD 1111",
            canonical_payee="Joe's Coffee",
        )
        record_payee_normalization_example(
            account_id=1,
            raw_payee_key="sq joes coffee 000123",
            raw_memo_key="card 1111",
            raw_payee_sample="SQ *JOES COFFEE 000123",
            raw_memo_sample="CARD 1111",
            canonical_payee="Joe's Coffee",
        )

        rules = list_payee_normalization_rules(account_id=1, keys=[], min_use_count=2)

        self.assertTrue(any(rule["canonical_payee"] == "Joe's Coffee" for rule in rules))
