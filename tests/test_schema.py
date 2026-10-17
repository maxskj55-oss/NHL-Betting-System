"""These tests exist to fail loudly if the API's response shape changes."""

import copy
import unittest
from datetime import datetime, timezone

from nhl_betting.snapshots import SchemaError, build_rows, validate_events, validate_odds
from tests.helpers import load_fixture

AS_OF = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.odds = load_fixture("odds_sample.json")
        self.events = load_fixture("events_sample.json")

    def test_fixtures_pass(self):
        validate_odds(self.odds)
        validate_events(self.events)

    def _expect_error(self, mutate):
        odds = copy.deepcopy(self.odds)
        mutate(odds)
        with self.assertRaises(SchemaError):
            build_rows(odds, AS_OF, ("fanduel", "pinnacle"), ("h2h", "spreads", "totals"))

    def test_missing_event_field(self):
        for key in ("id", "commence_time", "home_team", "away_team", "bookmakers"):
            with self.subTest(key=key):
                self._expect_error(lambda o: o[0].pop(key))

    def test_missing_outcome_price(self):
        self._expect_error(lambda o: o[0]["bookmakers"][0]["markets"][0]["outcomes"][0].pop("price"))

    def test_price_not_a_number(self):
        def mutate(o):
            o[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = "-135"
        self._expect_error(mutate)

    def test_totals_without_point(self):
        def mutate(o):
            o[0]["bookmakers"][0]["markets"][2]["outcomes"][0].pop("point")
        self._expect_error(mutate)

    def test_bad_timestamp(self):
        def mutate(o):
            o[0]["commence_time"] = "tonight"
        self._expect_error(mutate)

    def test_not_a_list(self):
        with self.assertRaises(SchemaError):
            validate_odds({"message": "error"})
        with self.assertRaises(SchemaError):
            validate_events({"message": "error"})

    def test_event_missing_field(self):
        events = copy.deepcopy(self.events)
        del events[1]["home_team"]
        with self.assertRaises(SchemaError):
            validate_events(events)


if __name__ == "__main__":
    unittest.main()
