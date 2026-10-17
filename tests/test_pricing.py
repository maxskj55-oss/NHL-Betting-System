"""Settlement rules (CLAUDE.md "Math"): OT and shootout count; shootout = +1 goal; whole totals push."""

import json
from pathlib import Path

import pytest

from nhl_betting.games import parse_games
from nhl_betting.pricing import (LOSS, OVER, PUSH, UNDER, WIN, SettlementError, settle_spread,
                                 settle_total, settlement_score, total_outcome)

NHL = Path(__file__).parent / "fixtures" / "nhl"


def real_game(day, game_id):
    games = parse_games(json.loads((NHL / f"score_{day}.json").read_text()), day)
    return next(g for g in games if g.game_id == game_id)


def score_of(g):
    return settlement_score(g.home_score, g.away_score, g.last_period)


def test_real_shootout_game_already_credits_the_winner():
    # SJS 3 @ NSH 2, decided in a shootout (2-2 after OT). The API's final score
    # includes the shootout goal, so the total is exactly 2 + 2 + 1 = 5.
    g = real_game("2026-10-15", 2026020115)
    assert g.last_period == "SO"
    s = score_of(g)
    assert (s.home, s.away, s.ot_so, s.total) == (2, 3, "SO", 5)


def test_real_scoreless_shootout_total_is_one():
    # DET 0 @ MTL 1 SO: 0-0 through OT, so the total settles at exactly 1.
    g = real_game("2026-10-10", 2026020075)
    s = score_of(g)
    assert (s.home, s.away, s.ot_so, s.total) == (1, 0, "SO", 1)
    assert total_outcome(s.total, 0.5) == OVER and total_outcome(s.total, 1.5) == UNDER


def test_every_real_final_settles():
    for path in NHL.glob("score_*.json"):
        day = path.stem.split("_", 1)[1]
        for g in parse_games(json.loads(path.read_text()), day):
            if g.is_final:
                s = score_of(g)
                if g.last_period in ("OT", "SO"):
                    assert abs(s.margin_home) == 1


@pytest.mark.parametrize("line,expected", [(5, PUSH), (5.0, PUSH), (5.5, UNDER), (4.5, OVER)])
def test_whole_number_total_pushes_on_real_shootout_game(line, expected):
    s = score_of(real_game("2026-10-15", 2026020115))   # total 5 including the shootout goal
    assert total_outcome(s.total, line) == expected


def test_shootout_goal_decides_a_total():
    # Without the shootout goal SJS@NSH would total 4 (under 4.5); with it, 5 (over).
    s = score_of(real_game("2026-10-15", 2026020115))
    assert settle_total(s.total, 4.5, "Over") == WIN
    assert settle_total(s.total, 4.5, "Under") == LOSS
    assert settle_total(s.total, 5, "Over") == PUSH and settle_total(s.total, 5, "Under") == PUSH


def test_puck_line_in_ot_and_shootout():
    for day, gid in (("2026-10-15", 2026020108), ("2026-10-15", 2026020115)):  # OT, SO
        s = score_of(real_game(day, gid))
        winner_margin = abs(s.margin_home)
        assert settle_spread(winner_margin, -1.5) == LOSS       # favourite -1.5 can't cover by 1
        assert settle_spread(-winner_margin, +1.5) == WIN       # loser +1.5 always covers


def test_puck_line_regulation_and_push_math():
    assert settle_spread(2, -1.5) == WIN
    assert settle_spread(-2, 1.5) == LOSS
    assert settle_spread(1, -1) == PUSH   # whole-number spread (not used for ±1.5) pushes


@pytest.mark.parametrize("h,a,period", [(3, 3, "SO"), (2, 2, "REG"), (4, 2, "OT"), (5, 3, "SO")])
def test_impossible_scores_raise(h, a, period):
    with pytest.raises(SettlementError):
        settlement_score(h, a, period)


def test_unknown_period_and_side_raise():
    with pytest.raises(SettlementError):
        settlement_score(3, 2, "2OT")
    with pytest.raises(ValueError):
        settle_total(5, 5.5, "Home")
