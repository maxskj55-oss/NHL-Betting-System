import csv
import io
import json
import unittest
from unittest import mock

from nhl_betting import mismatch_report as mr
from nhl_betting.sheets import read_tab
from nhl_betting.snapshots import COLUMNS


class RealDataTest(unittest.TestCase):
    def setUp(self):
        if not mr.FIXTURE.exists():
            self.skipTest("real fixture missing")
        self.rows = mr._rows_from_fixture()

    def test_oct17_counts(self):
        s = mr.summarize(mr.compare_games(self.rows))
        self.assertEqual(s.games, 14)
        self.assertEqual((s.totals_differ, s.totals_compared), (7, 14))
        self.assertEqual(dict(s.totals_gaps), {0.5: 7})
        self.assertEqual((s.spreads_differ, s.spreads_compared), (2, 14))
        self.assertEqual(s.favorite_flips, 1)
        self.assertEqual(s.any_differ, 8)

    def test_repeated_pull_not_double_counted(self):
        later = [dict(r, snapshot_time_utc="2026-10-17T20:00:00Z") for r in self.rows]
        s = mr.summarize(mr.compare_games(self.rows + later))
        self.assertEqual(s.games, 14)
        self.assertEqual(s.totals_differ, 7)

    def test_latest_snapshot_wins(self):
        # A later pull where FanDuel moves every total to Pinnacle's number.
        pin = {r["odds_event_id"]: r["line"] for r in self.rows
               if r["book"] == "pinnacle" and r["market"] == "totals"}
        later = []
        for r in self.rows:
            r2 = dict(r, snapshot_time_utc="2026-10-17T20:00:00Z")
            if r["book"] == "fanduel" and r["market"] == "totals" and r["odds_event_id"] in pin:
                r2["line"] = pin[r["odds_event_id"]]
            later.append(r2)
        self.assertEqual(mr.summarize(mr.compare_games(self.rows + later)).totals_differ, 0)

    def test_csv_and_sheet_value_types_agree(self):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(self.rows)
        from_csv = list(csv.DictReader(io.StringIO(buf.getvalue())))  # all strings
        from_sheet = [dict(r, line=float(r["line"]) if r["line"] != "" else "") for r in self.rows]
        a = mr.summarize(mr.compare_games(from_csv))
        b = mr.summarize(mr.compare_games(from_sheet))
        self.assertEqual((a.totals_differ, a.spreads_differ), (b.totals_differ, b.spreads_differ))

    def test_report_lists_tampa(self):
        text = mr.render(mr.compare_games(self.rows))
        self.assertIn("| 2026-10-17 | Vancouver Canucks @ Tampa Bay Lightning | 6.5 / 6.5 | -1.5 / -2.5 |", text)

    def test_dry_run_makes_no_calls(self):
        with mock.patch.object(mr, "authorized_session", side_effect=AssertionError("network")):
            self.assertEqual(mr.main(["--dry-run"]), 0)


class EmptyTest(unittest.TestCase):
    def test_no_rows(self):
        self.assertIn("| Games (latest snapshot each) | 0 |", mr.render([]))


class ReadTabTest(unittest.TestCase):
    def test_pads_short_rows(self):
        class R:
            status_code = 200
            def json(self):
                return {"values": [["a", "b", "c"], [1, 2], [3, 4, 5]]}
        session = mock.Mock()
        session.request.return_value = R()
        self.assertEqual(read_tab(session, "id", "Odds_Snapshots"),
                         [{"a": 1, "b": 2, "c": ""}, {"a": 3, "b": 4, "c": 5}])
        url = session.request.call_args[0][1]
        self.assertIn("%27Odds_Snapshots%27", url)


if __name__ == "__main__":
    unittest.main()
