import unittest

from nhl_betting.config import load_config
from nhl_betting.odds_api import BudgetError, OddsApiError, Usage, check_budget


class BudgetTest(unittest.TestCase):
    def test_config_pull_costs_3_credits(self):
        self.assertEqual(load_config().odds_api.credits_per_pull, 3)

    def test_ok_when_under_budget(self):
        check_budget(Usage(used=100, remaining=400, last=0), cost=3, monthly_budget=300)

    def test_ok_when_landing_exactly_on_budget(self):
        check_budget(Usage(used=297, remaining=203, last=0), cost=3, monthly_budget=300)

    def test_fails_when_pull_would_exceed_budget(self):
        with self.assertRaises(BudgetError):
            check_budget(Usage(used=298, remaining=202, last=0), cost=3, monthly_budget=300)

    def test_fails_when_account_nearly_empty(self):
        with self.assertRaises(BudgetError):
            check_budget(Usage(used=10, remaining=2, last=0), cost=3, monthly_budget=300)


class UsageHeadersTest(unittest.TestCase):
    def test_parses_headers(self):
        u = Usage.from_headers({"x-requests-used": "12", "x-requests-remaining": "488",
                                "x-requests-last": "3"})
        self.assertEqual((u.used, u.remaining, u.last), (12, 488, 3))

    def test_last_is_optional(self):
        u = Usage.from_headers({"x-requests-used": "12", "x-requests-remaining": "488"})
        self.assertIsNone(u.last)

    def test_missing_headers_fail(self):
        with self.assertRaises(OddsApiError):
            Usage.from_headers({"x-requests-used": "12"})


if __name__ == "__main__":
    unittest.main()
