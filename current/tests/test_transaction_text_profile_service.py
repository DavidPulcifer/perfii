from unittest import TestCase

from app.services.transaction_text_profile_service import (
    build_transaction_text_profile,
    build_transaction_text_profile_from_row,
    canonicalize_transaction_text,
    merchant_cluster_signature,
)


class TransactionTextProfileServiceTests(TestCase):
    def test_profile_preserves_account_suffix_and_reference_number(self) -> None:
        profile = build_transaction_text_profile(
            "Online Transfer to SAV ...0101 - transaction#: 000000001"
        )

        self.assertEqual(profile.raw_text, "Online Transfer to SAV ...0101 - transaction#: 000000001")
        self.assertEqual(profile.direction, "to")
        self.assertIn("online", profile.generic_tokens)
        self.assertIn("transfer", profile.generic_tokens)
        self.assertIn("sav", profile.account_type_hints)
        self.assertEqual(profile.account_suffixes, ("0101",))
        self.assertEqual(profile.reference_numbers, ("000000001",))
        self.assertNotIn("0101", profile.merchant_tokens)
        self.assertNotIn("000000001", profile.merchant_tokens)

    def test_profile_detects_checking_transfer_direction_and_suffix(self) -> None:
        profile = build_transaction_text_profile(
            "Online Transfer from CHK ...0202 - transaction#: 00000000002"
        )

        self.assertEqual(profile.direction, "from")
        self.assertIn("chk", profile.account_type_hints)
        self.assertEqual(profile.account_suffixes, ("0202",))
        self.assertEqual(profile.reference_numbers, ("00000000002",))

    def test_profile_detects_hyphenated_ending_in_suffix(self) -> None:
        profile = build_transaction_text_profile("Payment to Example Bank card ending in - 0303")

        self.assertEqual(profile.direction, "to")
        self.assertIn("card", profile.account_type_hints)
        self.assertEqual(profile.account_suffixes, ("0303",))
        self.assertNotIn("0303", profile.merchant_tokens)

    def test_profile_keeps_unlabeled_merchant_numbers_as_merchant_evidence(self) -> None:
        profile = build_transaction_text_profile("SQ *JOES COFFEE 000123")

        self.assertEqual(profile.reference_numbers, ())
        self.assertEqual(profile.account_suffixes, ())
        self.assertIn("joes", profile.merchant_tokens)
        self.assertIn("coffee", profile.merchant_tokens)
        self.assertIn("000123", profile.merchant_tokens)

    def test_profile_classifies_labeled_authorization_number_as_reference(self) -> None:
        profile = build_transaction_text_profile("Debit Card Purchase Sample Market auth 000777")

        self.assertIn("debit", profile.generic_tokens)
        self.assertIn("card", profile.generic_tokens)
        self.assertIn("purchase", profile.generic_tokens)
        self.assertIn("market", profile.merchant_tokens)
        self.assertEqual(profile.reference_numbers, ("000777",))
        self.assertNotIn("000777", profile.merchant_tokens)

    def test_profile_from_row_combines_payee_memo_and_name(self) -> None:
        profile = build_transaction_text_profile_from_row({
            "payee": "ACH Deposit",
            "memo": "Employer Payroll trace 000888",
            "name": "Ignored Name",
        })

        self.assertIn("employer", profile.merchant_tokens)
        self.assertIn("payroll", profile.merchant_tokens)
        self.assertIn("ignored", profile.merchant_tokens)
        self.assertEqual(profile.reference_numbers, ("000888",))

    def test_canonicalize_keeps_digits_instead_of_deleting_them(self) -> None:
        self.assertEqual(
            canonicalize_transaction_text("Online Transfer to SAV ...0101"),
            "online transfer to sav 0101",
        )

    def test_merchant_cluster_signature_collapses_marketplace_suffix(self) -> None:
        profile = build_transaction_text_profile("AMAZON MKTPL*DEMO00001")

        cluster = merchant_cluster_signature(profile)

        self.assertIsNotNone(cluster)
        self.assertEqual(cluster.signature, "amazon mktpl")
        self.assertEqual(cluster.tokens, ("amazon", "mktpl"))

    def test_merchant_cluster_signature_keeps_amazon_variants_separate(self) -> None:
        marketplace = merchant_cluster_signature(build_transaction_text_profile("AMAZON MKTPL*DEMO00001"))
        dotcom = merchant_cluster_signature(build_transaction_text_profile("Amazon.com*ABC"))
        prime = merchant_cluster_signature(build_transaction_text_profile("AMAZON PRIME*ABC"))

        self.assertEqual(marketplace.signature, "amazon mktpl")
        self.assertEqual(dotcom.signature, "amazon com")
        self.assertEqual(prime.signature, "amazon prime")

    def test_merchant_cluster_signature_rejects_weak_processor_only_tokens(self) -> None:
        self.assertIsNone(merchant_cluster_signature(build_transaction_text_profile("SQ *Vendor 123")))
        self.assertIsNone(merchant_cluster_signature(build_transaction_text_profile("SP Vendor")))
        self.assertIsNone(merchant_cluster_signature(build_transaction_text_profile("PayPal Vendor 12345")))
