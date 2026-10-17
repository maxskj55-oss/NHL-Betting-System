"""NHL team lookup: any source's team name -> NHL three-letter code.

The Odds API uses full names ("Montreal Canadiens"); the NHL web API uses
three-letter codes ("MTL"). Matching goes through normalize(), which drops
accents, punctuation and case, so "Montréal" and "St. Louis" variants match.
Add new spellings to ALIASES when a run warns about an unknown name.
"""

from __future__ import annotations

import re
import unicodedata

# 2026-27 season: 32 teams. code -> official name
TEAMS: dict[str, str] = {
    "ANA": "Anaheim Ducks",
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames",
    "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montréal Canadiens",
    "NSH": "Nashville Predators",
    "NJD": "New Jersey Devils",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SJS": "San Jose Sharks",
    "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals",
    "WPG": "Winnipeg Jets",
}

# Other spellings seen in sources -> code. Accent/punctuation variants are
# already handled by normalize(); list only genuinely different names.
ALIASES: dict[str, str] = {
    "Saint Louis Blues": "STL",
    "Utah Hockey Club": "UTA",   # 2024-25 name
    "Utah HC": "UTA",
    "LA Kings": "LAK",
    "Tampa Bay Bolts": "TBL",
}


def normalize(name: str) -> str:
    """'Montréal Canadiens' -> 'montreal canadiens'; 'St. Louis' -> 'st louis'."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return " ".join(text.split())


_LOOKUP: dict[str, str] = {}
for _code, _name in TEAMS.items():
    _LOOKUP[normalize(_name)] = _code
    _LOOKUP[normalize(_code)] = _code
for _alias, _code in ALIASES.items():
    _LOOKUP[normalize(_alias)] = _code


def team_code(name: str) -> str | None:
    """Return the NHL code for a team name or code, or None if unknown."""
    return _LOOKUP.get(normalize(name))
