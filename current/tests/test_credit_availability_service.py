from unittest import TestCase

from app.services.credit_availability_service import get_credit_budget_metrics


class CreditAvailabilityServiceTests(TestCase):
    def test_owed_card_available_to_allocate_matches_existing_formula(self) -> None:
        metrics = get_credit_budget_metrics(
            credit_limit_cents=100000,
            account_total_cents=-20000,
            envelope_balance_cents=30000,
        )

        self.assertEqual(metrics["owed_total_cents"], 20000)
        self.assertEqual(metrics["credit_balance_cents"], 0)
        self.assertEqual(metrics["available_to_allocate_cents"], 50000)

    def test_positive_credit_balance_is_tracked_without_increasing_capacity(self) -> None:
        metrics = get_credit_budget_metrics(
            credit_limit_cents=100000,
            account_total_cents=2500,
            envelope_balance_cents=0,
        )

        self.assertEqual(metrics["owed_total_cents"], 0)
        self.assertEqual(metrics["credit_balance_cents"], 2500)
        self.assertEqual(metrics["available_to_allocate_cents"], 100000)
