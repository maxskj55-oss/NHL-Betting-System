"""Checks run against real /odds responses saved in tests/fixtures/real/.

Each file is a raw response copied from a run's output/raw/ artifact. These tests
catch quirks in FanDuel's and Pinnacle's real data that handmade fixtures miss.
"""

import unittest
from datetime import timedelta
from pathlib import Path

from nhl_betting.config import load_config
from nhl_betting.snapshots import build_rows, parse_time, validate_odds
from nhl_betting.teams import team_code
from tests.helpers import FIXTURES

REAL = sorted((FIXTURES / "real").glob("*.json"))
CFG = load_config().odds_api


def _load(path: Path):
    import json
    return json.loads(path.read_text())


@unittest.skipUnless(REAL, "no real fixtures yet: copy a raw response into tests/fixtures/real/")
class RealFixtureTest(unittest.TestCase):
    def test_each_file(self):
        for path in REAL:
            with self.subTest(file=path.name):
                odds = _load(path)
                validate_odds(odds)
                self.assertTrue(odds, "empty response")
                first = min(parse_time(e["commence_time"]) for e in odds)
                rows, dropped = build_rows(odds, first - timedelta(hours=1),
                                           CFG.bookmakers, CFG.markets)
                self.assertTrue(rows)
                self.assertEqual(dropped, 0)

                for ev in odds:
                    for side in ("home_team", "away_team"):
                        self.assertIsNotNone(team_code(ev[side]),
                                             f"unknown team {ev[side]!r}: add to teams.py")
                    teams = {ev["home_team"], ev["away_team"]}
                    for bk in ev["bookmakers"]:
                        for mk in bk["markets"]:
                            where = f"{ev['away_team']} @ {ev['home_team']} {bk['key']} {mk['key']}"
                            names = [o["name"] for o in mk["outcomes"]]
                            self.assertEqual(len(names), 2, where)
                            if mk["key"] in ("h2h", "spreads"):
                                self.assertEqual(set(names), teams, where)
                            if mk["key"] == "spreads":
                                pts = sorted(o["point"] for o in mk["outcomes"])
                                self.assertEqual(pts[0], -pts[1], where)
                            if mk["key"] == "totals":
                                self.assertEqual(set(names), {"Over", "Under"}, where)
                                self.assertEqual(len({o["point"] for o in mk["outcomes"]}), 1, where)


if __name__ == "__main__":
    unittest.main()
