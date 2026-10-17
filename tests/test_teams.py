import unittest

from nhl_betting.teams import ALIASES, TEAMS, normalize, team_code
from tests.helpers import load_fixture


class TeamsTest(unittest.TestCase):
    def test_32_teams_unique(self):
        self.assertEqual(len(TEAMS), 32)
        self.assertEqual(len(set(TEAMS.values())), 32)
        self.assertEqual(len({normalize(n) for n in TEAMS.values()}), 32)

    def test_every_official_name_and_code_resolves(self):
        for code, name in TEAMS.items():
            with self.subTest(code=code):
                self.assertEqual(team_code(name), code)
                self.assertEqual(team_code(code), code)
                self.assertEqual(team_code(code.lower()), code)

    def test_spelling_variants(self):
        cases = {
            "Montreal Canadiens": "MTL",
            "Montréal Canadiens": "MTL",
            "MONTREAL CANADIENS": "MTL",
            "St Louis Blues": "STL",
            "St. Louis Blues": "STL",
            "Saint Louis Blues": "STL",
            "Utah Hockey Club": "UTA",
            "Utah Mammoth": "UTA",
            "  Vegas  Golden Knights ": "VGK",
        }
        for name, code in cases.items():
            with self.subTest(name=name):
                self.assertEqual(team_code(name), code)

    def test_aliases_point_at_real_codes(self):
        for alias, code in ALIASES.items():
            self.assertIn(code, TEAMS, alias)

    def test_unknown_and_ambiguous_names(self):
        self.assertIsNone(team_code("Arizona Coyotes"))
        self.assertIsNone(team_code("New York"))  # Rangers or Islanders: never guess
        self.assertIsNone(team_code(""))

    def test_fixture_teams_all_known(self):
        for ev in load_fixture("events_sample.json"):
            for side in ("home_team", "away_team"):
                self.assertIsNotNone(team_code(ev[side]), ev[side])


if __name__ == "__main__":
    unittest.main()
