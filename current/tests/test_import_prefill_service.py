from __future__ import annotations

from unittest import TestCase

from app.db import get_db, unit_of_work
from app.repositories import accounts_repo, import_prefill_repo, remainder_intents_repo, splits_repo, transactions_repo
from app.services.import_prefill_service import (
    build_import_prefills,
    normalize_text,
    select_import_prefill,
    split_signature,
)
from tests.helpers import FinanceAppTestCase


CURRENT_ACCOUNT = 1
OTHER_ACCOUNT = 2
ENV_A = 10
ENV_B = 11
ENV_C = 12


def standard_example(*, posted_at: str, envelope_id: int, amount_cents: int = -5000, payee: str = "Acme Gym") -> dict:
    return {
        "id": abs(hash((posted_at, envelope_id, amount_cents))) % 100000,
        "account_id": CURRENT_ACCOUNT,
        "ttype": "expense" if amount_cents < 0 else "income",
        "amount_cents": amount_cents,
        "posted_at": posted_at,
        "payee": payee,
        "memo": "monthly membership",
        "splits": [{"envelope_id": envelope_id, "amount_cents": amount_cents}],
    }


def split_example(*, posted_at: str = "2026-04-10") -> dict:
    return {
        "id": 200,
        "account_id": CURRENT_ACCOUNT,
        "ttype": "expense",
        "amount_cents": -10000,
        "posted_at": posted_at,
        "payee": "Warehouse Store",
        "memo": "groceries household",
        "splits": [
            {"envelope_id": ENV_A, "amount_cents": -7000},
            {"envelope_id": ENV_B, "amount_cents": -3000},
        ],
    }


def split_remainder_example(*, posted_at: str, amount_cents: int, fixed_cents: int = -6000) -> dict:
    remainder_cents = int(amount_cents) - int(fixed_cents)
    return {
        "id": abs(hash(("remainder", posted_at, amount_cents))) % 100000,
        "account_id": CURRENT_ACCOUNT,
        "ttype": "expense",
        "amount_cents": amount_cents,
        "posted_at": posted_at,
        "payee": "Warehouse Store",
        "memo": "groceries household",
        "splits": [
            {"envelope_id": ENV_A, "amount_cents": fixed_cents},
            {"envelope_id": ENV_B, "amount_cents": remainder_cents},
        ],
        "remainder_intent": {"envelope_id": ENV_B, "amount_cents": remainder_cents},
    }


def transfer_example(*, posted_at: str = "2026-04-15", amount_cents: int = -4614) -> dict:
    return {
        "id": 300,
        "account_id": CURRENT_ACCOUNT,
        "account_name": "Checking",
        "ttype": "transfer_out",
        "amount_cents": amount_cents,
        "posted_at": posted_at,
        "payee": "Savings Account",
        "memo": "automatic savings transfer",
        "splits": [{"envelope_id": ENV_A, "amount_cents": amount_cents}],
        "paired_account_id": OTHER_ACCOUNT,
        "paired_account_name": "Savings Account",
        "paired_transaction": {
            "id": 301,
            "account_id": OTHER_ACCOUNT,
            "account_name": "Savings Account",
            "ttype": "transfer_in",
            "amount_cents": abs(amount_cents),
            "posted_at": posted_at,
            "payee": "Checking",
            "memo": "automatic savings transfer",
            "splits": [
                {"envelope_id": ENV_B, "amount_cents": 3000},
                {"envelope_id": ENV_C, "amount_cents": abs(amount_cents) - 3000},
            ],
        },
    }


def transfer_in_example(*, posted_at: str = "2026-04-15", amount_cents: int = 4614) -> dict:
    return {
        "id": 310,
        "account_id": CURRENT_ACCOUNT,
        "account_name": "Checking",
        "ttype": "transfer_in",
        "amount_cents": amount_cents,
        "posted_at": posted_at,
        "payee": "Savings Account",
        "memo": "automatic savings transfer",
        "splits": [
            {"envelope_id": ENV_B, "amount_cents": 3000},
            {"envelope_id": ENV_C, "amount_cents": amount_cents - 3000},
        ],
        "paired_account_id": OTHER_ACCOUNT,
        "paired_account_name": "Savings Account",
        "paired_transaction": {
            "id": 311,
            "account_id": OTHER_ACCOUNT,
            "account_name": "Savings Account",
            "ttype": "transfer_out",
            "amount_cents": -abs(amount_cents),
            "posted_at": posted_at,
            "payee": "Checking",
            "memo": "automatic savings transfer",
            "splits": [{"envelope_id": ENV_A, "amount_cents": -abs(amount_cents)}],
        },
    }


class ImportPrefillServiceUnitTests(TestCase):
    def test_normalize_text_strips_noise_ids_and_case(self) -> None:
        self.assertEqual(normalize_text("POS Debit ACME GYM 123456"), "acme gym")

    def test_split_signature_is_stable(self) -> None:
        self.assertEqual(
            split_signature([
                {"envelope_id": 2, "amount_cents": -300},
                {"envelope_id": 1, "amount_cents": -700},
            ]),
            ((1, -700), (2, -300)),
        )

    def test_single_envelope_prefill_prefers_recent_run_over_old_majority(self) -> None:
        history = [
            standard_example(posted_at="2025-01-01", envelope_id=ENV_A),
            standard_example(posted_at="2025-02-01", envelope_id=ENV_A),
            standard_example(posted_at="2025-03-01", envelope_id=ENV_A),
            standard_example(posted_at="2025-04-01", envelope_id=ENV_A),
            standard_example(posted_at="2026-03-01", envelope_id=ENV_B),
            standard_example(posted_at="2026-04-01", envelope_id=ENV_B),
        ]

        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -5000, "payee": "ACME Gym", "memo": "membership"},
            0,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transaction_type"], "expense")
        self.assertIsNone(prefill["single_envelope_id"])
        self.assertEqual(prefill["splits"], [])
        self.assertEqual(prefill["remainder_envelope_id"], ENV_B)
        self.assertIn("latest_allocation_run", prefill["debug_reason_codes"])
        self.assertIn("merchant_identity_match", prefill["debug_reason_codes"])
        self.assertIn("single_envelope_history", prefill["debug_reason_codes"])

    def test_future_dated_history_after_import_date_is_ignored(self) -> None:
        history = [
            standard_example(posted_at="2026-03-01", envelope_id=ENV_A),
            standard_example(posted_at="2026-06-01", envelope_id=ENV_B),
            standard_example(posted_at="2026-06-15", envelope_id=ENV_B),
        ]

        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -5000, "payee": "ACME Gym"},
            0,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["remainder_envelope_id"], ENV_A)

    def test_tied_standard_patterns_leave_row_blank(self) -> None:
        history = [
            standard_example(posted_at="2026-04-01", envelope_id=ENV_A),
            standard_example(posted_at="2026-04-02", envelope_id=ENV_B),
        ]

        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -5000, "payee": "ACME Gym"},
            0,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertFalse(prefill["prefill"])
        self.assertIn("tied_or_ambiguous_pattern", prefill["debug_reason_codes"])
        evidence = prefill["prediction_debug"]["evidence"]
        self.assertEqual(evidence["withheld_reason"], "ambiguous_competing_candidates")
        self.assertEqual(evidence["candidate_group_count"], 2)
        self.assertEqual(evidence["candidate_count"], 2)
        self.assertEqual(evidence["winning_candidate"]["support_count"], 1)
        self.assertEqual(len(evidence["competing_candidates"]), 1)
        self.assertIn("score_components", evidence["winning_candidate"])
        self.assertEqual(
            evidence["winning_candidate"]["matched_raw_profile_facts"]["merchant_tokens"],
            ["acme", "gym"],
        )

    def test_transfer_like_row_is_not_standard_prefilled_from_amount_only(self) -> None:
        history = [
            standard_example(posted_at="2026-04-01", envelope_id=ENV_A, amount_cents=-10000),
        ]

        prefill = select_import_prefill(
            {
                "posted_at": "2026-05-01",
                "amount_cents": -10000,
                "payee": "Online Transfer to SAV ...0101",
                "memo": "transaction#: 000000001",
            },
            0,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertFalse(prefill["prefill"])
        self.assertIn("no_compatible_pattern", prefill["debug_reason_codes"])

    def test_multi_split_without_stability_uses_largest_envelope_as_remainder(self) -> None:
        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -10000, "payee": "Warehouse Store", "memo": "groceries"},
            2,
            CURRENT_ACCOUNT,
            [split_example()],
        )

        self.assertTrue(prefill["prefill"])
        self.assertIsNone(prefill["single_envelope_id"])
        self.assertEqual(prefill["splits"], [])
        self.assertEqual(prefill["remainder_envelope_id"], ENV_A)

    def test_multi_split_prefill_preserves_stable_splits_and_uses_variable_remainder(self) -> None:
        history = [
            split_remainder_example(posted_at="2026-04-01", amount_cents=-10000, fixed_cents=-6000),
            split_remainder_example(posted_at="2026-04-15", amount_cents=-12000, fixed_cents=-6000),
        ]
        for row in history:
            row.pop("remainder_intent", None)

        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -15000, "payee": "Warehouse Store", "memo": "groceries"},
            2,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["splits"], [{"envelope_id": ENV_A, "amount_cents": -6000}])
        self.assertEqual(prefill["remainder_envelope_id"], ENV_B)

    def test_remainder_prefill_keeps_fixed_splits_and_remainder_selector(self) -> None:
        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -15000, "payee": "Warehouse Store", "memo": "groceries"},
            2,
            CURRENT_ACCOUNT,
            [
                split_remainder_example(posted_at="2026-04-01", amount_cents=-10000),
                split_remainder_example(posted_at="2026-04-15", amount_cents=-12000),
            ],
        )

        self.assertTrue(prefill["prefill"])
        self.assertIsNone(prefill["single_envelope_id"])
        self.assertEqual(prefill["splits"], [{"envelope_id": ENV_A, "amount_cents": -6000}])
        self.assertEqual(prefill["remainder_envelope_id"], ENV_B)
        self.assertIn("remainder_pattern", prefill["debug_reason_codes"])
        self.assertIn("merchant_identity_match", prefill["debug_reason_codes"])
        self.assertIn("repeated_pattern", prefill["debug_reason_codes"])

    def test_explicit_remainder_history_teaches_exact_split_rows(self) -> None:
        exact = split_example(posted_at="2026-04-01")
        explicit = split_remainder_example(posted_at="2026-04-15", amount_cents=-12000, fixed_cents=-7000)

        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -15000, "payee": "Warehouse Store", "memo": "groceries"},
            2,
            CURRENT_ACCOUNT,
            [exact, explicit],
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["splits"], [{"envelope_id": ENV_A, "amount_cents": -7000}])
        self.assertEqual(prefill["remainder_envelope_id"], ENV_B)

    def test_transfer_remainder_prefill_keeps_fixed_splits_and_remainder_selectors(self) -> None:
        history = [transfer_example(posted_at="2026-04-15", amount_cents=-10000)]
        history[0]["splits"] = [
            {"envelope_id": ENV_A, "amount_cents": -3000},
            {"envelope_id": ENV_B, "amount_cents": -7000},
        ]
        history[0]["remainder_intent"] = {"envelope_id": ENV_B, "amount_cents": -7000}
        history[0]["paired_transaction"]["splits"] = [
            {"envelope_id": ENV_B, "amount_cents": 2000},
            {"envelope_id": ENV_C, "amount_cents": 8000},
        ]
        history[0]["paired_transaction"]["remainder_intent"] = {"envelope_id": ENV_C, "amount_cents": 8000}

        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -13000, "payee": "Savings Account", "memo": "automatic savings transfer"},
            3,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transfer"]["current_account_splits"], [
            {"envelope_id": ENV_A, "amount_cents": -3000},
        ])
        self.assertEqual(prefill["transfer"]["current_account_remainder_envelope_id"], ENV_B)
        self.assertEqual(prefill["transfer"]["other_account_splits"], [
            {"envelope_id": ENV_B, "amount_cents": 2000},
        ])
        self.assertEqual(prefill["transfer"]["other_account_remainder_envelope_id"], ENV_C)
        self.assertIn("remainder_pattern", prefill["debug_reason_codes"])

    def test_transfer_prefill_returns_other_account_and_both_legs(self) -> None:
        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -4614, "payee": "Savings Account", "memo": "automatic savings transfer"},
            3,
            CURRENT_ACCOUNT,
            [transfer_example()],
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transaction_type"], "transfer_out")
        self.assertEqual(prefill["transfer"]["other_account_id"], OTHER_ACCOUNT)
        self.assertEqual(prefill["transfer"]["other_account_name"], "Savings Account")
        self.assertEqual(prefill["transfer"]["current_account_splits"], [])
        self.assertEqual(prefill["transfer"]["current_account_remainder_envelope_id"], ENV_A)
        self.assertEqual(prefill["transfer"]["other_account_splits"], [])
        self.assertEqual(prefill["transfer"]["other_account_remainder_envelope_id"], ENV_B)

    def test_transfer_prefill_uses_account_suffix_evidence(self) -> None:
        history = [transfer_example(posted_at="2026-04-15", amount_cents=-10000)]
        history[0]["payee"] = "Transfer"
        history[0]["memo"] = ""
        history[0]["paired_account_name"] = "Example Bank - 0101"
        history[0]["paired_transaction"]["account_name"] = "Example Bank - 0101"
        history[0]["paired_transaction"]["payee"] = "Transfer"
        history[0]["paired_transaction"]["memo"] = ""

        prefill = select_import_prefill(
            {
                "posted_at": "2026-05-01",
                "amount_cents": -10000,
                "payee": "Online Transfer to SAV ...0101",
                "memo": "transaction#: 000000001",
            },
            3,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transaction_type"], "transfer_out")
        self.assertEqual(prefill["transfer"]["other_account_id"], OTHER_ACCOUNT)
        self.assertIn("matched_account_suffix", prefill["debug_reason_codes"])
        self.assertIn("matched_direction", prefill["debug_reason_codes"])

    def test_transfer_prefill_prefers_explicit_account_identity_over_support_volume(self) -> None:
        def transfer_history(
            *,
            idx: int,
            other_account_id: int,
            other_account_name: str,
            other_account_type: str,
            amount_cents: int,
            posted_at: str,
            payee: str,
            memo: str = "",
        ) -> dict:
            return {
                "id": idx,
                "account_id": CURRENT_ACCOUNT,
                "account_name": "Example Bank Checking - 0202",
                "ttype": "transfer_out",
                "amount_cents": amount_cents,
                "posted_at": posted_at,
                "payee": payee,
                "memo": memo,
                "splits": [{"envelope_id": ENV_A, "amount_cents": amount_cents}],
                "paired_account_id": other_account_id,
                "paired_account_name": other_account_name,
                "paired_account_type": other_account_type,
                "paired_transaction": {
                    "id": idx + 10000,
                    "account_id": other_account_id,
                    "account_name": other_account_name,
                    "account_type": other_account_type,
                    "ttype": "transfer_in",
                    "amount_cents": abs(amount_cents),
                    "posted_at": posted_at,
                    "payee": "Example Bank Checking - 0202",
                    "memo": memo,
                    "splits": [{"envelope_id": ENV_B, "amount_cents": abs(amount_cents)}],
                },
            }

        sample_investing_history = [
            transfer_history(
                idx=400 + offset,
                other_account_id=3000,
                other_account_name="Sample Investing",
                other_account_type="investment",
                amount_cents=-2500,
                posted_at=f"2026-04-{10 + offset:02d}",
                payee="Transfer to Sample Investing",
                memo="portfolio contribution",
            )
            for offset in range(10)
        ]
        example_bank_0303_history = transfer_history(
            idx=303,
            other_account_id=303,
            other_account_name="Example Bank Card - 0303",
            other_account_type="card",
            amount_cents=-12345,
            posted_at="2025-11-15",
            payee="Payment to Example Bank card ending in - 0303",
        )

        prefill = select_import_prefill(
            {
                "posted_at": "2026-04-29",
                "amount_cents": -6789,
                "payee": "Payment to Example Bank card ending in - 0303",
            },
            60,
            CURRENT_ACCOUNT,
            [*sample_investing_history, example_bank_0303_history],
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transaction_type"], "transfer_out")
        self.assertEqual(prefill["transfer"]["other_account_id"], 303)
        self.assertIn("matched_account_suffix", prefill["debug_reason_codes"])
        self.assertIn("strong_account_identity", prefill["debug_reason_codes"])
        self.assertEqual(
            prefill["prediction_debug"]["evidence"]["matched_account_suffixes"],
            ["0303"],
        )

    def test_transfer_prefill_suffix_match_beats_same_institution_support(self) -> None:
        def example_bank_card_payment(
            *,
            idx: int,
            account_id: int,
            account_name: str,
            suffix: str,
            amount_cents: int,
            posted_at: str,
        ) -> dict:
            return {
                "id": idx,
                "account_id": CURRENT_ACCOUNT,
                "account_name": "Example Bank Checking - 0202",
                "ttype": "transfer_out",
                "amount_cents": amount_cents,
                "posted_at": posted_at,
                "payee": f"Payment to Example Bank card ending in - {suffix}",
                "memo": "",
                "splits": [{"envelope_id": ENV_A, "amount_cents": amount_cents}],
                "paired_account_id": account_id,
                "paired_account_name": account_name,
                "paired_account_type": "credit_card",
                "paired_acctid": f"SYNTHETIC-CARD-{suffix}",
                "paired_transaction": {
                    "id": idx + 10000,
                    "account_id": account_id,
                    "account_name": account_name,
                    "account_type": "credit_card",
                    "acctid": f"SYNTHETIC-CARD-{suffix}",
                    "ttype": "transfer_in",
                    "amount_cents": abs(amount_cents),
                    "posted_at": posted_at,
                    "payee": "Example Bank Checking - 0202",
                    "memo": "",
                    "splits": [{"envelope_id": ENV_B, "amount_cents": abs(amount_cents)}],
                },
            }

        example_bank_0303_history = [
            example_bank_card_payment(
                idx=500 + offset,
                account_id=303,
                account_name="Example Bank Card - 0303",
                suffix="0303",
                amount_cents=-(10000 + offset),
                posted_at=f"2026-02-{2 + offset:02d}",
            )
            for offset in range(4)
        ]
        example_bank_0404_history = example_bank_card_payment(
            idx=404,
            account_id=404,
            account_name="Example Bank Card - 0404",
            suffix="0404",
            amount_cents=-4321,
            posted_at="2025-02-18",
        )

        prefill = select_import_prefill(
            {
                "posted_at": "2026-02-02",
                "amount_cents": -4321,
                "payee": "Payment to Example Bank card ending in - 0404",
            },
            164,
            CURRENT_ACCOUNT,
            [*example_bank_0303_history, example_bank_0404_history],
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transaction_type"], "transfer_out")
        self.assertEqual(prefill["transfer"]["other_account_id"], 404)
        self.assertIn("matched_account_suffix", prefill["debug_reason_codes"])
        evidence = prefill["prediction_debug"]["evidence"]
        self.assertEqual(evidence["matched_account_suffixes"], ["0404"])

    def test_transfer_prefill_does_not_use_unstructured_merchant_numbers_as_suffixes(self) -> None:
        history = [transfer_example(posted_at="2026-04-15", amount_cents=-10000)]
        history[0]["payee"] = "Transfer"
        history[0]["memo"] = ""
        history[0]["paired_account_name"] = "Example Bank - 0505"
        history[0]["paired_transaction"]["account_name"] = "Example Bank - 0505"

        prefill = select_import_prefill(
            {
                "posted_at": "2026-05-01",
                "amount_cents": -9999,
                "payee": "SQ SAMPLE CAFE 000555",
                "memo": "",
            },
            3,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertFalse(prefill["prefill"])

    def test_transfer_prefill_uses_remainders_for_changed_full_leg_amount(self) -> None:
        history = [transfer_example(posted_at="2026-04-15", amount_cents=-5000)]
        history[0]["paired_transaction"]["splits"] = [{"envelope_id": ENV_B, "amount_cents": 5000}]

        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -2500, "payee": "Savings Account", "memo": "automatic savings transfer"},
            3,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transfer"]["current_account_splits"], [])
        self.assertEqual(prefill["transfer"]["current_account_remainder_envelope_id"], ENV_A)
        self.assertEqual(prefill["transfer"]["other_account_splits"], [])
        self.assertEqual(prefill["transfer"]["other_account_remainder_envelope_id"], ENV_B)
        self.assertIn("remainder_pattern", prefill["debug_reason_codes"])

    def test_transfer_in_prefill_returns_other_account_and_both_legs(self) -> None:
        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": 4614, "payee": "Savings Account", "memo": "automatic savings transfer"},
            4,
            CURRENT_ACCOUNT,
            [transfer_in_example()],
        )

        self.assertTrue(prefill["prefill"])
        self.assertEqual(prefill["transaction_type"], "transfer_in")
        self.assertEqual(prefill["transfer"]["other_account_id"], OTHER_ACCOUNT)
        self.assertEqual(prefill["transfer"]["current_account_splits"], [])
        self.assertEqual(prefill["transfer"]["current_account_remainder_envelope_id"], ENV_B)
        self.assertEqual(prefill["transfer"]["other_account_splits"], [])
        self.assertEqual(prefill["transfer"]["other_account_remainder_envelope_id"], ENV_A)

    def test_build_import_prefills_accepts_injected_history(self) -> None:
        prefills = build_import_prefills(
            [{"posted_at": "2026-05-01", "amount": "-50.00", "payee": "ACME Gym"}],
            CURRENT_ACCOUNT,
            history_rows=[standard_example(posted_at="2026-04-01", envelope_id=ENV_A)],
        )

        self.assertEqual(len(prefills), 1)
        self.assertTrue(prefills[0]["prefill"])
        self.assertEqual(prefills[0]["remainder_envelope_id"], ENV_A)

    def test_prediction_feedback_contributes_to_learning_score_evidence(self) -> None:
        history = [standard_example(posted_at="2026-04-01", envelope_id=ENV_A)]
        history[0]["learning_example_id"] = 123
        history[0]["prediction_feedback"] = {"accepted": 2, "modified": 0, "rejected": 0}

        prefill = select_import_prefill(
            {"posted_at": "2026-05-01", "amount_cents": -5000, "payee": "ACME Gym"},
            0,
            CURRENT_ACCOUNT,
            history,
        )

        evidence = prefill["prediction_debug"]["evidence"]
        self.assertTrue(prefill["prefill"])
        self.assertEqual(evidence["learning_example_id"], 123)
        self.assertEqual(evidence["score_components"]["prediction_feedback"], 1.0)
        self.assertEqual(evidence["winning_candidate"]["learning_example_id"], 123)
        self.assertEqual(evidence["winning_candidate"]["support_count"], 1)
        self.assertEqual(evidence["winning_candidate"]["evidence_quality"], "low")
        self.assertEqual(evidence["winning_candidate"]["evidence_source"], "final_transaction_history")
        self.assertEqual(evidence["candidate_group_count"], 1)
        self.assertEqual(evidence["candidate_count"], 1)

    def test_standard_prefill_rejects_noisy_reference_text_without_identity(self) -> None:
        history = [
            standard_example(
                posted_at="2026-03-25",
                envelope_id=9,
                amount_cents=-3000,
                payee="Sample Licensing Office",
            ),
            standard_example(
                posted_at="2025-04-02",
                envelope_id=9,
                amount_cents=-3500,
                payee="Sample Licensing Office",
            ),
            standard_example(
                posted_at="2023-12-11",
                envelope_id=ENV_A,
                amount_cents=-1200,
                payee="Fictional Contact",
            ),
        ]
        history[-1]["memo"] = "REF 000111 WEB ID: 0002222222"

        prefill = select_import_prefill(
            {
                "posted_at": "2026-06-10",
                "amount_cents": -1400,
                "payee": "PEER PAYMENT DEMO-000333 WEB ID: 0002222222",
            },
            2,
            CURRENT_ACCOUNT,
            history,
        )

        self.assertFalse(prefill["prefill"])
        self.assertIn("no_compatible_pattern", prefill["debug_reason_codes"])


class ImportPrefillRepositoryTests(FinanceAppTestCase):
    def test_history_query_returns_splits_for_inserted_transaction(self) -> None:
        db = get_db()
        account = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        envelope = db.execute("SELECT id FROM envelopes ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(account)
        self.assertIsNotNone(envelope)
        account_id = int(account["id"])
        envelope_id = int(envelope["id"])

        with unit_of_work() as tx_db:
            tx_id = transactions_repo.insert_transaction(
                db=tx_db,
                account_id=account_id,
                ttype="expense",
                amount_cents=-4321,
                posted_at="2026-05-17",
                payee="FIN045 Test Payee",
                memo="prefill repo coverage",
                fitid="fin045-prefill-repo-test",
            )
            splits_repo.insert_split(
                db=tx_db,
                transaction_id=tx_id,
                envelope_id=envelope_id,
                amount_cents=-4321,
            )

        rows = import_prefill_repo.list_import_prefill_history(
            account_id=account_id,
            date_from="2026-05-17",
            date_to="2026-05-17",
            limit=20,
        )
        row = next(r for r in rows if int(r["id"]) == int(tx_id))

        self.assertEqual(row["payee"], "FIN045 Test Payee")
        self.assertEqual(row["splits"], [
            {
                "transaction_id": tx_id,
                "envelope_id": envelope_id,
                "amount_cents": -4321,
                "envelope_name": row["splits"][0]["envelope_name"],
                "locked_account_id": row["splits"][0]["locked_account_id"],
                "envelope_archived_at": row["splits"][0]["envelope_archived_at"],
            }
        ])

    def test_history_query_returns_remainder_intent_metadata(self) -> None:
        db = get_db()
        account = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        envelopes = db.execute("SELECT id FROM envelopes ORDER BY id LIMIT 2").fetchall()
        self.assertIsNotNone(account)
        self.assertGreaterEqual(len(envelopes), 1)
        account_id = int(account["id"])
        fixed_envelope_id = int(envelopes[0]["id"])
        remainder_envelope_id = int(envelopes[-1]["id"])

        with unit_of_work() as tx_db:
            tx_id = transactions_repo.insert_transaction(
                db=tx_db,
                account_id=account_id,
                ttype="expense",
                amount_cents=-10000,
                posted_at="2026-05-18",
                payee="FIN048 Remainder Payee",
                memo="prefill repo remainder coverage",
                fitid="fin048-prefill-remainder-test",
            )
            splits_repo.insert_split(
                db=tx_db,
                transaction_id=tx_id,
                envelope_id=fixed_envelope_id,
                amount_cents=-6000,
            )
            splits_repo.insert_split(
                db=tx_db,
                transaction_id=tx_id,
                envelope_id=remainder_envelope_id,
                amount_cents=-4000,
            )
            remainder_intents_repo.replace_remainder_intent(
                db=tx_db,
                transaction_id=tx_id,
                envelope_id=remainder_envelope_id,
                amount_cents=-4000,
            )

        rows = import_prefill_repo.list_import_prefill_history(
            account_id=account_id,
            date_from="2026-05-18",
            date_to="2026-05-18",
            limit=20,
        )
        row = next(r for r in rows if int(r["id"]) == int(tx_id))

        self.assertEqual(row["remainder_intent"]["transaction_id"], tx_id)
        self.assertEqual(row["remainder_intent"]["envelope_id"], remainder_envelope_id)
        self.assertEqual(row["remainder_intent"]["amount_cents"], -4000)

    def test_history_query_keeps_split_history_without_remainder_intent_table(self) -> None:
        db = get_db()
        account = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        envelope = db.execute("SELECT id FROM envelopes ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(account)
        self.assertIsNotNone(envelope)
        account_id = int(account["id"])
        envelope_id = int(envelope["id"])

        with unit_of_work() as tx_db:
            tx_id = transactions_repo.insert_transaction(
                db=tx_db,
                account_id=account_id,
                ttype="expense",
                amount_cents=-2500,
                posted_at="2026-05-19",
                payee="FIN048 No Metadata Table",
                memo="prefill repo missing metadata table coverage",
                fitid="fin048-prefill-no-remainder-table-test",
            )
            splits_repo.insert_split(
                db=tx_db,
                transaction_id=tx_id,
                envelope_id=envelope_id,
                amount_cents=-2500,
            )
            tx_db.execute("DROP TABLE transaction_remainder_intents")

        rows = import_prefill_repo.list_import_prefill_history(
            account_id=account_id,
            date_from="2026-05-19",
            date_to="2026-05-19",
            limit=20,
        )
        row = next(r for r in rows if int(r["id"]) == int(tx_id))

        self.assertEqual(row["remainder_intent"], None)
        self.assertEqual([(s["envelope_id"], s["amount_cents"]) for s in row["splits"]], [(envelope_id, -2500)])

        with unit_of_work() as tx_db:
            remainder_intents_repo.replace_remainder_intent(
                db=tx_db,
                transaction_id=tx_id,
                envelope_id=envelope_id,
                amount_cents=-2500,
            )
        self.assertIsNone(remainder_intents_repo.get_remainder_intent(tx_id))

    def test_combined_history_prefers_high_quality_raw_learning_example_for_transfer(self) -> None:
        db = get_db()
        envelope = db.execute("SELECT id FROM envelopes ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(envelope)
        envelope_id = int(envelope["id"])

        with unit_of_work() as tx_db:
            checking_id = accounts_repo.insert_account(
                {
                    "name": "FIN077 Checking",
                    "account_type": "bank",
                    "bankid": "FIN077",
                    "acctid": "000111",
                },
                db=tx_db,
            )
            savings_id = accounts_repo.insert_account(
                {
                    "name": "Example Bank - 0101",
                    "account_type": "bank",
                    "bankid": "EXAMPLE-BANK",
                    "acctid": "SYNTHETIC-ACCOUNT-0101",
                },
                db=tx_db,
            )
            out_id = transactions_repo.insert_transaction(
                db=tx_db,
                account_id=checking_id,
                ttype="transfer_out",
                amount_cents=-12500,
                posted_at="2026-06-20",
                payee="Example Bank - 0101",
                memo="Savings transfer",
                fitid="fin077-n-transfer-out",
            )
            in_id = transactions_repo.insert_transaction(
                db=tx_db,
                account_id=savings_id,
                ttype="transfer_in",
                amount_cents=12500,
                posted_at="2026-06-20",
                payee="FIN077 Checking",
                memo="Savings transfer",
                fitid="fin077-n-transfer-in",
            )
            tx_db.execute("UPDATE transactions SET xfer_pair_id=? WHERE id=?", (in_id, out_id))
            tx_db.execute("UPDATE transactions SET xfer_pair_id=? WHERE id=?", (out_id, in_id))
            splits_repo.insert_split(
                db=tx_db,
                transaction_id=out_id,
                envelope_id=envelope_id,
                amount_cents=-12500,
            )
            tx_db.execute(
                """
                INSERT INTO transaction_learning_examples(
                    account_id, transaction_id, source, evidence_quality, dedupe_key,
                    posted_at, amount_cents, raw_payee, raw_memo, raw_profile_json,
                    final_payee, final_memo, final_profile_json, transaction_type,
                    transfer_other_account_id, splits_json, remainder_intent_json,
                    decision_json, evidence_json, created_at, updated_at
                ) VALUES (
                    ?, ?, 'backfill', 'high', 'fin077-n-high-transfer',
                    '2026-06-20', -12500, 'Online Transfer',
                    'to SAV ...0101 trace 000777', '{}',
                    'Example Bank - 0101', 'Savings transfer', '{}', 'transfer_out',
                    ?, ?, '{}', '{}', '{}',
                    '2026-06-21T12:00:00+00:00', '2026-06-21T12:00:00+00:00'
                )
                """,
                (
                    checking_id,
                    out_id,
                    savings_id,
                    '[{"envelope_id":%d,"amount_cents":-12500}]' % envelope_id,
                ),
            )

        prefills = build_import_prefills(
            [
                {
                    "posted_at": "2026-06-21",
                    "amount_cents": -12500,
                    "payee": "Online Transfer",
                    "memo": "to SAV ...0101 trace 000777",
                }
            ],
            checking_id,
        )

        self.assertTrue(prefills[0]["prefill"])
        self.assertEqual(prefills[0]["transaction_type"], "transfer_out")
        self.assertEqual(prefills[0]["transfer"]["other_account_id"], savings_id)
        self.assertIn("high_quality_learning_example", prefills[0]["debug_reason_codes"])
        self.assertIn("matched_account_suffix", prefills[0]["debug_reason_codes"])
        evidence = prefills[0]["prediction_debug"]["evidence"]
        self.assertEqual(evidence["evidence_source"], "backfill")
        self.assertEqual(evidence["evidence_quality"], "high")
        self.assertEqual(evidence["matched_account_suffixes"], ["0101"])
        self.assertIn("score_components", evidence)

    def test_combined_history_falls_back_to_final_history_for_low_quality_example(self) -> None:
        db = get_db()
        account = db.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        envelope = db.execute("SELECT id FROM envelopes ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(account)
        self.assertIsNotNone(envelope)
        account_id = int(account["id"])
        envelope_id = int(envelope["id"])

        with unit_of_work() as tx_db:
            tx_id = transactions_repo.insert_transaction(
                db=tx_db,
                account_id=account_id,
                ttype="expense",
                amount_cents=-1500,
                posted_at="2026-06-18",
                payee="Streaming Service",
                memo="Monthly",
                fitid="fin077-n-low-final-fallback",
            )
            splits_repo.insert_split(
                db=tx_db,
                transaction_id=tx_id,
                envelope_id=envelope_id,
                amount_cents=-1500,
            )
            tx_db.execute(
                """
                INSERT INTO transaction_learning_examples(
                    account_id, transaction_id, source, evidence_quality, dedupe_key,
                    posted_at, amount_cents, raw_payee, raw_memo, raw_profile_json,
                    final_payee, final_memo, final_profile_json, transaction_type,
                    splits_json, remainder_intent_json, decision_json, evidence_json,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'transaction_edit', 'low', 'fin077-n-low-fallback',
                    '2026-06-18', -1500, 'BANK CARD 9999', '', '{}',
                    'Streaming Service', 'Monthly', '{}', 'expense',
                    ?, '{}', '{}', '{}',
                    '2026-06-21T12:00:00+00:00', '2026-06-21T12:00:00+00:00'
                )
                """,
                (
                    account_id,
                    tx_id,
                    '[{"envelope_id":%d,"amount_cents":-1500}]' % envelope_id,
                ),
            )

        prefills = build_import_prefills(
            [
                {
                    "posted_at": "2026-06-20",
                    "amount_cents": -1500,
                    "payee": "Streaming Service",
                    "memo": "Monthly",
                }
            ],
            account_id,
        )

        self.assertTrue(prefills[0]["prefill"])
        self.assertEqual(prefills[0]["remainder_envelope_id"], envelope_id)
        evidence = prefills[0]["prediction_debug"]["evidence"]
        self.assertEqual(evidence["evidence_source"], "final_transaction_history")
        self.assertEqual(evidence["evidence_quality"], "low")
