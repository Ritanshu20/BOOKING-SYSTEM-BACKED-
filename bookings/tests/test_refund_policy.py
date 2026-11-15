"""Phase 7: refund tiers at exact boundaries and Decimal rounding."""

from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from bookings import refund_policy


class RefundTierTests(SimpleTestCase):
    def test_tier_boundaries_with_the_default_policy(self):
        """>=48h: 100%, 24h to <48h: 50%, <24h: 0%, checked to the second."""
        cases = [
            (timedelta(days=10), 100),
            (timedelta(hours=48), 100),
            (timedelta(hours=48) - timedelta(seconds=1), 50),
            (timedelta(hours=30), 50),
            (timedelta(hours=24), 50),
            (timedelta(hours=24) - timedelta(seconds=1), 0),
            (timedelta(minutes=1), 0),
            (timedelta(0), 0),
        ]
        for notice, expected_percent in cases:
            with self.subTest(notice=notice):
                self.assertEqual(refund_policy.refund_percent_for_notice(notice), expected_percent)

    @override_settings(
        REFUND_POLICY_TIERS=[
            {"min_hours_before_start": 72, "refund_percent": 90},
            {"min_hours_before_start": 0, "refund_percent": 10},
        ]
    )
    def test_policy_is_read_from_settings(self):
        """Changing REFUND_POLICY_TIERS changes the refund without code changes."""
        self.assertEqual(refund_policy.refund_percent_for_notice(timedelta(hours=72)), 90)
        self.assertEqual(refund_policy.refund_percent_for_notice(timedelta(hours=71)), 10)


class RefundAmountTests(SimpleTestCase):
    def test_amounts_are_rounded_half_up_to_the_cent(self):
        """Half-cent results round up: 49.995 -> 50.00, 0.005 -> 0.01, 50.005 -> 50.01."""
        cases = [
            (Decimal("99.99"), 50, Decimal("50.00")),
            (Decimal("0.01"), 50, Decimal("0.01")),
            (Decimal("100.01"), 50, Decimal("50.01")),
            (Decimal("301.00"), 50, Decimal("150.50")),
            (Decimal("0.03"), 50, Decimal("0.02")),
        ]
        for paid, percent, expected in cases:
            with self.subTest(paid=paid, percent=percent):
                self.assertEqual(refund_policy.refund_amount(paid, percent), expected)

    def test_full_and_zero_refunds_are_exact(self):
        """100% returns exactly the paid amount and 0% returns 0.00."""
        self.assertEqual(refund_policy.refund_amount(Decimal("123.45"), 100), Decimal("123.45"))
        self.assertEqual(refund_policy.refund_amount(Decimal("123.45"), 0), Decimal("0.00"))

    def test_refund_never_exceeds_the_paid_amount(self):
        """Even a misconfigured percentage above 100 is capped at the paid amount."""
        self.assertEqual(refund_policy.refund_amount(Decimal("10.00"), 150), Decimal("10.00"))

    def test_results_are_decimals_with_two_places(self):
        """Money stays Decimal; no float ever appears."""
        amount = refund_policy.refund_amount(Decimal("80"), 50)

        self.assertIsInstance(amount, Decimal)
        self.assertEqual(amount.as_tuple().exponent, -2)
