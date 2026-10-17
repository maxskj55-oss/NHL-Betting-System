import copy
import unittest
from collections import Counter
from datetime import datetime, timezone

from nhl_betting.snapshots import COLUMNS, american_to_decimal, build_rows
from tests.helpers import load_fixture

BOOKS = ("fanduel", "pinnacle")
MARKETS = ("h2h", "spreads", "totals")
BEFORE_ALL = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)


class BuildRowsTest(unittest.TestCase):
    def setUp(self):
        self.odds = load_fixture("odds_sample.json")

    def test_row_count_and_columns(self):
        rows, dropped = build_rows(self.odds, BEFORE_ALL, BOOKS, MARKETS)
        # 3 games x 2 books x 3 markets x 2 sides = 36, minus Pinnacle's missing puck line (2)
        self.assertEqual(len(rows), 34)
        self.assertEqual(dropped, 0)
        for r in rows:
            self.assertEqual(tuple(r.keys()), COLUMNS)

    def test_counts_by_book_and_market(self):
        rows, _ = build_rows(self.odds, BEFORE_ALL, BOOKS, MARKETS)
        counts = Counter((r["book"], r["market"]) for r in rows)
        self.assertEqual(counts[("fanduel", "spreads")], 6)
        self.assertEqual(counts[("pinnacle", "spreads")], 4)
        self.assertEqual(counts[("pinnacle", "totals")], 6)

    def test_values_for_one_row(self):
        rows, _ = build_rows(self.odds, BEFORE_ALL, BOOKS, MARKETS)
        r = next(r for r in rows if r["book"] == "fanduel" and r["market"] == "spreads"
                 and r["side"] == "Toronto Maple Leafs")
        self.assertEqual(r["snapshot_time_utc"], "2026-10-17T20:00:00Z")
        self.assertEqual(r["odds_event_id"], "a1f0c3d2e4b5a6978812345678abcd01")
        self.assertEqual(r["start_time_utc"], "2026-10-17T23:00:00Z")
        self.assertEqual(r["home_team"], "Toronto Maple Leafs")
        self.assertEqual(r["away_team"], "Boston Bruins")
        self.assertEqual(r["line"], "-1.5")
        self.assertEqual(r["price_american"], 172)
        self.assertEqual(r["price_decimal"], 2.72)
        self.assertEqual(r["book_last_update_utc"], "2026-10-17T19:45:12Z")
        self.assertEqual(r["pre_start_check"], "TRUE")

    def test_line_formats(self):
        rows, _ = build_rows(self.odds, BEFORE_ALL, BOOKS, MARKETS)
        self.assertTrue(all(r["line"] == "" for r in rows if r["market"] == "h2h"))
        lines = {r["line"] for r in rows if r["market"] == "totals"}
        self.assertEqual(lines, {"6.5", "6"})
        sides = {r["side"] for r in rows if r["market"] == "totals"}
        self.assertEqual(sides, {"Over", "Under"})

    def test_started_games_are_dropped(self):
        # 00:30Z: Toronto game (23:00Z) has started; the other two haven't.
        as_of = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        rows, dropped = build_rows(self.odds, as_of, BOOKS, MARKETS)
        self.assertEqual(dropped, 12)
        self.assertEqual(len(rows), 22)
        self.assertNotIn("a1f0c3d2e4b5a6978812345678abcd01", {r["odds_event_id"] for r in rows})

    def test_snapshot_exactly_at_puck_drop_is_dropped(self):
        as_of = datetime(2026, 10, 17, 23, 0, tzinfo=timezone.utc)
        rows, dropped = build_rows(self.odds, as_of, BOOKS, MARKETS)
        self.assertEqual(dropped, 12)

    def test_other_books_and_markets_ignored(self):
        odds = copy.deepcopy(self.odds)
        extra = copy.deepcopy(odds[0]["bookmakers"][0])
        extra["key"] = "draftkings"
        odds[0]["bookmakers"].append(extra)
        odds[0]["bookmakers"][0]["markets"].append(
            {"key": "h2h_3_way", "outcomes": [{"name": "Draw", "price": 300}]})
        rows, _ = build_rows(odds, BEFORE_ALL, BOOKS, MARKETS)
        self.assertEqual(len(rows), 34)


class PriceTest(unittest.TestCase):
    def test_american_to_decimal(self):
        self.assertEqual(american_to_decimal(-135), 1.7407)
        self.assertEqual(american_to_decimal(114), 2.14)
        self.assertEqual(american_to_decimal(100), 2.0)
        self.assertEqual(american_to_decimal(-100), 2.0)


if __name__ == "__main__":
    unittest.main()
