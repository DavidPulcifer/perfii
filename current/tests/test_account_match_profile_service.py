import unittest

from app.services.account_match_profile_service import (
    build_account_match_profile,
    build_account_match_profiles,
    find_profiles_by_suffix,
)


class AccountMatchProfileServiceTest(unittest.TestCase):
    def test_profile_extracts_fictional_label_and_account_id_suffix(self):
        profile = build_account_match_profile({
            "id": 7,
            "name": "Example Bank - 0101",
            "acct_key": "synthetic:example-bank-0101",
            "bankid": "000000000",
            "acctid": "SYNTHETIC-ACCOUNT-0101",
            "account_type": "bank",
        })

        self.assertEqual(profile.account_id, 7)
        self.assertEqual(profile.name_tokens, ("example", "bank", "0101"))
        self.assertEqual(profile.label_suffixes, ("0101",))
        self.assertEqual(profile.acctid_suffixes, ("0101",))
        self.assertEqual(profile.all_suffixes, ("0101",))
        self.assertEqual(profile.type_aliases, ("bank",))
        self.assertEqual(profile.institution_tokens, ("example",))

    def test_profile_builds_savings_type_aliases(self):
        profile = build_account_match_profile({
            "id": 9,
            "name": "Emergency Savings",
            "acct_key": "acct:emergency-savings",
            "account_type": "savings",
        })

        self.assertEqual(profile.type_aliases, ("savings", "saving", "sav"))
        self.assertEqual(profile.institution_tokens, ("emergency",))

    def test_profile_builds_checking_type_aliases_from_name(self):
        profile = build_account_match_profile({
            "id": 3,
            "name": "Household CHK 0202",
            "account_type": "bank",
        })

        self.assertEqual(profile.label_suffixes, ("0202",))
        self.assertIn("checking", profile.type_aliases)
        self.assertIn("chk", profile.type_aliases)

    def test_find_profiles_by_suffix_uses_structured_suffixes_only(self):
        profiles = build_account_match_profiles([
            {"id": 1, "name": "Coffee 0505 Rewards", "account_type": "card"},
            {
                "id": 2,
                "name": "Example Bank - 0101",
                "acctid": "SYNTHETIC-ACCOUNT-0101",
                "account_type": "bank",
            },
        ])

        matches = find_profiles_by_suffix(profiles, "0101")

        self.assertEqual([profile.account_id for profile in matches], [2])

    def test_identifier_suffix_uses_last_four_digits(self):
        profile = build_account_match_profile({
            "id": 4,
            "name": "Brokerage",
            "acctid": "SYNTHETIC-ACCOUNT-4321",
            "account_type": "investment",
        })

        self.assertEqual(profile.acctid_suffixes, ("4321",))
        self.assertEqual(profile.type_aliases, ("investment", "invest", "brokerage"))


if __name__ == "__main__":
    unittest.main()
