"""Odds API event -> NHL game_id mapping, using today's real odds and NHL fixtures."""

import json
from datetime import timedelta
from pathlib import Path

from nhl_betting.games import parse_games
from nhl_betting.matching import OddsEvent, events_from_snapshots, match_events
from nhl_betting.snapshots import parse_time

FIX = Path(__file__).parent / "fixtures"


def real_odds_events():
    odds = json.loads((FIX / "real" / "odds_20261017T153438Z.json").read_text())
    return [OddsEvent(e["id"], e["home_team"], e["away_team"], parse_time(e["commence_time"]))
            for e in odds]


def oct17_games():
    return parse_games(json.loads((FIX / "nhl" / "score_2026-10-17.json").read_text()), "2026-10-17")


def test_every_real_odds_event_maps_to_exactly_one_game():
    events, games = real_odds_events(), oct17_games()
    matched, problems = match_events(events, games, 3)
    assert problems == []
    assert len(matched) == len(events) == 14
    assert len({g.game_id for g in matched.values()}) == 14   # one-to-one
    assert matched["841c71c033cf05ad1313e3d5ecd2d9ab"].game_id == 2026020121   # NJD @ WSH


def test_unknown_team_reversed_teams_and_far_start_are_reported_not_guessed():
    games = oct17_games()
    wsh = next(g for g in games if g.home == "WSH")
    events = [
        OddsEvent("a", "Washington Kapitals", "New Jersey Devils", wsh.start_utc),
        OddsEvent("b", "New Jersey Devils", "Washington Capitals", wsh.start_utc),
        OddsEvent("c", "Washington Capitals", "New Jersey Devils", wsh.start_utc + timedelta(hours=5)),
    ]
    matched, problems = match_events(events, games, 3)
    assert matched == {}
    assert "unknown team name" in problems[0]
    assert "other way round" in problems[1]
    assert "no NHL game within 3 h" in problems[2]


def test_events_from_snapshots_dedupes_and_keeps_latest_start():
    rows = [
        {"snapshot_time_utc": "2026-10-17T15:00:00Z", "odds_event_id": "x", "home_team": "Boston Bruins",
         "away_team": "Buffalo Sabres", "start_time_utc": "2026-10-17T23:00:00Z"},
        {"snapshot_time_utc": "2026-10-17T18:00:00Z", "odds_event_id": "x", "home_team": "Boston Bruins",
         "away_team": "Buffalo Sabres", "start_time_utc": "2026-10-17T23:30:00Z"},
        {"snapshot_time_utc": "2026-10-17T18:00:00Z", "odds_event_id": "", "home_team": "?",
         "away_team": "?", "start_time_utc": "?"},
    ]
    events = events_from_snapshots(rows)
    assert [e.odds_event_id for e in events] == ["x"]
    assert events[0].start_utc == parse_time("2026-10-17T23:30:00Z")
