"""Step 4 grading: CLV worked by hand, line differences, settlement at the pick's line, parlays."""

from datetime import datetime, timezone

import pytest

from nhl_betting import grading, pricing
from nhl_betting.grading import Context, closes_by_game, closing_quote, grade_item
from nhl_betting.picks import PICK_GRADES_COLUMNS, PICKS_COLUMNS, PARLAYS_COLUMNS
from nhl_betting.pricing import FinalScore
from nhl_betting.tabs import MemoryTab, ProtectedColumnError

NOW = datetime(2026, 10, 18, 12, 17, tzinfo=timezone.utc)
GID = "2026020900"
HOME, AWAY = "Colorado Avalanche", "Pittsburgh Penguins"


def snap(book, market, side, line, price, at="2026-10-18T00:55:00Z", start="2026-10-18T01:10:00Z",
         event="ev1"):
    return {"snapshot_time_utc": at, "odds_event_id": event, "start_time_utc": start,
            "home_team": HOME, "away_team": AWAY, "book": book, "market": market, "side": side,
            "line": "" if line is None else line, "price_american": price,
            "price_decimal": round(pricing.decimal_odds(price), 4), "book_last_update_utc": at,
            "pre_start_check": True}


GAME_MAP = [{"odds_event_id": "ev1", "game_id": GID}]


def ctx(snapshots, home_score=4, away_score=2, ot_so="REG", void=None):
    games = {GID: grading.Game("2026-10-17", "COL", "PIT", FinalScore(home_score, away_score, ot_so))}
    return Context(games=games, closes=closes_by_game(snapshots, GAME_MAP),
                   void_games=void or {}, now=NOW)


# ---- CLV worked by hand -------------------------------------------------------
# Pinnacle closes COL -120 / PIT +100.
#   implied: 120/220 = 6/11 and 100/200 = 1/2; sum 23/22
#   no-vig: COL = (6/11)/(23/22) = 12/23, PIT = 11/23
# COL taken at -110 (decimal 21/11): CLV = 21/11 x 12/23 - 1 = 252/253 - 1 = -1/253
# PIT taken at +110 (decimal 2.1):   CLV = 2.1 x 11/23 - 1 = 0.1/23
ML_CLOSE = [snap("pinnacle", "h2h", HOME, None, -120), snap("pinnacle", "h2h", AWAY, None, 100),
            snap("fanduel", "h2h", HOME, None, -125), snap("fanduel", "h2h", AWAY, None, 105)]


def test_clv_by_hand_moneyline_uses_pinnacle():
    c = ctx(ML_CLOSE)
    col = grade_item(c, "claude", "20261017-01", "lean", GID, "moneyline", "COL", "", -110, 0.55, 0.5)
    pit = grade_item(c, "claude", "20261017-02", "lean", GID, "moneyline", "PIT", "", 110, 0.5, 0)
    assert col["close_prob"] == round(12 / 23, 6) and col["clv_book"] == "pinnacle"
    assert col["clv"] == round(-1 / 253, 6) == -0.003953
    assert pit["clv"] == round(0.1 / 23, 6) == 0.004348
    assert (col["close_pinnacle_price"], col["close_fanduel_price"]) == (-120, -125)
    assert col["result"] == "win" and col["return_per_unit"] == round(10 / 11, 6)
    assert pit["result"] == "loss" and pit["return_per_unit"] == -1.0
    assert col["note"] == "" and col["close_snapshot_utc"] == "2026-10-18T00:55:00Z"


def test_clv_function_matches_rulebook_formula():
    assert pricing.clv(-110, 12 / 23) == pytest.approx(-1 / 253)
    with pytest.raises(ValueError):
        pricing.clv(-110, 1.2)


# ---- Closing line differs from the pick's line ------------------------------
# Pick: Over 6.5 at -105 (decimal 41/21). Pinnacle closes at 6 (not comparable).
# FanDuel closes Over 6.5 -120 / Under 6.5 +100 -> Over no-vig 12/23.
# CLV = 41/21 x 12/23 - 1 = 492/483 - 1 = 9/483
TOTALS_CLOSE = [snap("pinnacle", "totals", "Over", 6, -115), snap("pinnacle", "totals", "Under", 6, -105),
                snap("fanduel", "totals", "Over", 6.5, -120), snap("fanduel", "totals", "Under", 6.5, 100)]


def test_pinnacle_line_differs_uses_fanduel_close_at_the_pick_line():
    row = grade_item(ctx(TOTALS_CLOSE), "claude", "20261017-03", "bet candidate", GID, "total",
                     "over", 6.5, -105, 0.56, 0.5)
    assert row["clv_book"] == "fanduel"
    assert row["clv"] == round(9 / 483, 6) == 0.018634
    assert row["close_pinnacle_line"] == 6 and row["close_fanduel_line"] == 6.5
    assert row["note"] == "pinnacle closing line 6; used fanduel no-vig close"


def test_no_close_at_the_pick_line_leaves_clv_blank_with_reason():
    both_at_6 = [snap("pinnacle", "totals", "Over", 6, -115), snap("pinnacle", "totals", "Under", 6, -105),
                 snap("fanduel", "totals", "Over", 6, -110), snap("fanduel", "totals", "Under", 6, -110)]
    row = grade_item(ctx(both_at_6), "claude", "20261017-03", "lean", GID, "total", "over", 6.5,
                     -105, 0.52, 0)
    assert row["clv"] == "" and row["close_prob"] == "" and row["clv_book"] == ""
    assert row["note"] == "no close at line 6.5 (pinnacle 6, fanduel 6)"
    assert row["result"] == "loss"          # still settled: 6 goals vs Over 6.5


def test_no_snapshot_at_all():
    row = grade_item(ctx([]), "claude", "20261017-01", "lean", GID, "moneyline", "COL", "", -110, 0.5, 0)
    assert row["clv"] == "" and row["note"] == "no pre-start snapshot" and row["result"] == "win"


def test_closing_snapshot_is_the_last_pre_start_one():
    early = [dict(r, snapshot_time_utc="2026-10-17T20:00:00Z", price_american=-150) for r in ML_CLOSE]
    after_start = [dict(r, snapshot_time_utc="2026-10-18T01:20:00Z", price_american=-300) for r in ML_CLOSE]
    q = closing_quote(closes_by_game(early + ML_CLOSE + after_start, GAME_MAP)[GID], "moneyline", "COL", None)
    assert q.snapshot_utc == "2026-10-18T00:55:00Z" and q.pinnacle == (None, -120)


# ---- Settled at the pick's own line ------------------------------------------
def test_settles_at_the_line_taken_not_the_last_snapshot_line():
    # 4-2 = 6 goals. Taken Over 6.5 (loss); the last snapshot's 6.0 would push.
    c = ctx(TOTALS_CLOSE)
    over65 = grade_item(c, "claude", "a", "lean", GID, "total", "over", 6.5, -105, 0.5, 0)
    under65 = grade_item(c, "claude", "b", "lean", GID, "total", "under", 6.5, -115, 0.5, 0)
    over6 = grade_item(c, "claude", "c", "lean", GID, "total", "over", 6, -110, 0.5, 0)
    assert (over65["result"], under65["result"], over6["result"]) == ("loss", "win", "push")
    assert over6["return_per_unit"] == 0.0


@pytest.mark.parametrize("side,line,home,away,ot,expected", [
    ("COL", -1.5, 4, 2, "REG", "win"),
    ("COL", -1.5, 3, 2, "OT", "loss"),
    ("PIT", 1.5, 3, 2, "SO", "win"),
    ("PIT", -1.5, 1, 3, "REG", "win"),
])
def test_puck_line_settles_at_pick_line(side, line, home, away, ot, expected):
    row = grade_item(ctx([], home, away, ot), "claude", "x", "lean", GID, "puck_line", side, line,
                     110, 0.5, 0)
    assert row["result"] == expected


def test_game_not_final_waits():
    c = ctx([])
    assert grade_item(c, "claude", "x", "lean", "2026020999", "moneyline", "COL", "", -110, 0.5, 0) is None


# ---- Parlays -----------------------------------------------------------------
def pick(pid, gid, market, side, line, price, label="bet candidate", **kw):
    return dict({"pick_id": pid, "game_id": gid, "market": market, "side": side, "line": line,
                 "price": price, "baseline_prob": 0.5, "claude_prob": 0.55, "label": label,
                 "stake_units": 0.5}, **kw)


def parlay_ctx():
    games = {"1": grading.Game("2026-10-17", "COL", "PIT", FinalScore(4, 2, "REG")),
             "3": grading.Game("2026-10-17", "BOS", "ANA", FinalScore(4, 2, "REG"))}  # 6 goals
    return Context(games=games, closes={}, void_games={"2": "2026-10-17"}, now=NOW)


PICKS = {"20261017-01": pick("20261017-01", "1", "moneyline", "COL", "", 100),
         "20261017-02": pick("20261017-02", "2", "moneyline", "NYR", "", -150),
         "20261017-03": pick("20261017-03", "3", "moneyline", "BOS", "", -110),
         "20261017-04": pick("20261017-04", "3", "total", "over", 6, -110),
         "20261017-05": pick("20261017-05", "1", "moneyline", "PIT", "", 120)}


def ticket(legs, price=500, **kw):
    return dict({"ticket_id": "T20261017-01", "leg_pick_ids": legs, "combined_price": price,
                 "stake_units": 0.25, "placed": "no"}, **kw)


def test_void_leg_drops_out_and_price_is_recomputed():
    rows = grading.grade_ticket(parlay_ctx(), "T20261017-01",
                                ticket("20261017-01, 20261017-02, 20261017-03"), PICKS)
    legs, t = rows[:-1], rows[-1]
    assert [r["group"] for r in rows] == ["parlay_leg"] * 3 + ["parlay"]
    assert [r["result"] for r in legs] == ["win", "void", "win"]
    assert legs[1]["note"] == "game postponed or cancelled"
    # Remaining legs: +100 (2.0) x -110 (21/11) = 42/11; the ticket's +500 no longer applies.
    assert t["result"] == "win" and t["return_per_unit"] == round(42 / 11 - 1, 6)
    assert "3 legs: 2 win, 1 void" in t["note"] and "remaining legs" in t["note"]
    assert t["item_id"] == "T20261017-01" and t["side"] == "20261017-01, 20261017-02, 20261017-03"


def test_pushed_leg_is_treated_as_void_and_noted():
    rows = grading.grade_ticket(parlay_ctx(), "T20261017-01", ticket("20261017-01,20261017-04"), PICKS)
    t = rows[-1]
    assert [r["result"] for r in rows[:-1]] == ["win", "push"]
    assert t["result"] == "win" and t["return_per_unit"] == 1.0     # +100 leg alone
    assert grading.PUSH_ASSUMPTION in t["note"]


def test_full_ticket_pays_its_own_price_and_a_loss_loses():
    win = grading.grade_ticket(parlay_ctx(), "T", ticket("20261017-01,20261017-03", price=270), PICKS)[-1]
    assert win["result"] == "win" and win["return_per_unit"] == 2.7
    placed = grading.grade_ticket(parlay_ctx(), "T", ticket("20261017-01,20261017-03", price=270,
                                                           placed="yes", actual_price=280), PICKS)[-1]
    assert placed["price_taken"] == 280 and placed["return_per_unit"] == 2.8
    lose = grading.grade_ticket(parlay_ctx(), "T", ticket("20261017-01,20261017-05"), PICKS)[-1]
    assert lose["result"] == "loss" and lose["return_per_unit"] == -1.0


def test_all_legs_void_makes_ticket_void():
    t = grading.grade_ticket(parlay_ctx(), "T", ticket("20261017-02"), PICKS)[-1]
    assert t["result"] == "void" and t["return_per_unit"] == 0.0


def test_ticket_waits_for_every_leg_and_skips_unknown_legs():
    c = parlay_ctx()
    c.games.pop("3")
    assert grading.grade_ticket(c, "T", ticket("20261017-01,20261017-03"), PICKS) is None
    assert grading.grade_ticket(parlay_ctx(), "T", ticket("20261017-01,20261017-99"), PICKS) is None


def test_ticket_clv_from_leg_clvs():
    games = {GID: grading.Game("2026-10-17", "COL", "PIT", FinalScore(4, 2, "REG"))}
    c = Context(games=games, closes=closes_by_game(ML_CLOSE + TOTALS_CLOSE, GAME_MAP), void_games={},
                now=NOW)
    picks = {"a": pick("a", GID, "moneyline", "PIT", "", 110), "b": pick("b", GID, "total", "over", 6.5, -105)}
    rows = grading.grade_ticket(c, "T", ticket("a,b"), picks)
    assert rows[0]["clv"] == round(0.1 / 23, 6) and rows[1]["clv"] == round(9 / 483, 6)
    assert rows[-1]["clv"] == pytest.approx((1 + 0.1 / 23) * (1 + 9 / 483) - 1, abs=2e-6)


# ---- Groups and the tab --------------------------------------------------------
def test_pick_rows_make_claude_and_placed_groups_and_test_note():
    picks = {"TEST-20261017-01": pick("TEST-20261017-01", GID, "moneyline", "COL", "", -110,
                                      placed="yes", actual_price=-105, claude_prob=""),
             "20261017-02": pick("20261017-02", GID, "moneyline", "PIT", "", 110, label="lean")}
    items = grading.pick_items(picks)
    assert [(i["group"], i["item_id"]) for i in items] == [
        ("claude", "TEST-20261017-01"), ("placed", "TEST-20261017-01"), ("claude", "20261017-02")]
    assert items[0]["price"] == -110 and items[1]["price"] == -105
    assert items[0]["prob"] == 0.5          # claude_prob blank -> baseline_prob
    assert items[2]["prob"] == 0.55 and items[0]["note"] == "test"


def test_best_available_label_is_graded_without_warning(caplog):
    picks = {"20261020-01": pick("20261020-01", GID, "moneyline", "COL", "", -120,
                                 label="best available", placed="yes")}
    with caplog.at_level("WARNING"):
        items = grading.pick_items(picks)
    assert [(i["group"], i["label"]) for i in items] == [("claude", "best available"),
                                                         ("placed", "best available")]
    assert "graded anyway" not in caplog.text


def test_unknown_pick_label_still_warns(caplog):
    picks = {"20261020-02": pick("20261020-02", GID, "moneyline", "COL", "", -120, label="steal")}
    with caplog.at_level("WARNING"):
        items = grading.pick_items(picks)
    assert len(items) == 1 and "graded anyway" in caplog.text


def test_best_available_is_not_an_auto_lean():
    base = {"game_date_et": "2026-10-17", "game_id": GID, "market": "moneyline", "line": "",
            "fanduel_price": -110, "baseline_prob": 0.5, "stake_units": 0}
    assert grading.auto_items([dict(base, side="COL", auto_lean="best available")]) == []


def test_auto_items_only_bet_candidates_and_leans():
    base = {"game_date_et": "2026-10-17", "game_id": GID, "market": "moneyline", "line": "",
            "fanduel_price": -110, "baseline_prob": 0.53, "stake_units": 0}
    rows = [dict(base, side="COL", auto_lean="lean"), dict(base, side="PIT", auto_lean="pass"),
            dict(base, market="total", side="over", auto_lean="pass: line mismatch"),
            dict(base, side="COL", market="", auto_lean="")]
    items = grading.auto_items(rows)
    assert [i["item_id"] for i in items] == [f"2026-10-17|{GID}|moneyline|COL"]
    assert items[0]["prob"] == 0.53 and items[0]["label"] == "lean"


def tabs_with(picks_rows=(), parlay_rows=(), archive_rows=()):
    from nhl_betting.morning import ARCHIVE_COLUMNS, GAME_MAP_COLUMNS, RESULTS_COLUMNS
    results = MemoryTab("Results", RESULTS_COLUMNS, rows=[{
        "game_id": GID, "game_date_et": "2026-10-17", "home": "COL", "away": "PIT",
        "home_score": 4, "away_score": 2, "ot_so": "REG"}])
    return dict(grades=MemoryTab("Pick_Grades", PICK_GRADES_COLUMNS),
                picks=MemoryTab("Picks", PICKS_COLUMNS, PICKS_COLUMNS, rows=picks_rows),
                parlays=MemoryTab("Parlays", PARLAYS_COLUMNS, PARLAYS_COLUMNS, rows=parlay_rows),
                archive=MemoryTab("Tonight_Archive", ARCHIVE_COLUMNS, rows=archive_rows),
                results=results, game_map=MemoryTab("Game_Map", GAME_MAP_COLUMNS, rows=GAME_MAP))


def test_apply_appends_once_and_never_writes_picks_or_parlays():
    tabs = tabs_with(picks_rows=[pick("TEST-20261017-01", GID, "moneyline", "COL", "", -110)])
    rep = grading.apply(snapshots=ML_CLOSE, void_games={}, now=NOW, **tabs)
    assert len(rep.rows) == 1 and tabs["grades"].appended[0]["note"] == "test"
    assert tabs["grades"].appended[0]["clv"] == round(-1 / 253, 6)
    again = grading.apply(snapshots=ML_CLOSE, void_games={}, now=NOW, **tabs)
    assert again.rows == [] and len(tabs["grades"].rows) == 1
    assert tabs["picks"].updated == [] and tabs["picks"].appended == []
    with pytest.raises(ProtectedColumnError):
        tabs["picks"].update_cells([(2, {"placed": "yes"})])
    with pytest.raises(ProtectedColumnError):
        tabs["parlays"].update_cells([(2, {"placed": "yes"})])


def test_placed_later_gets_its_own_row_on_a_later_run():
    tabs = tabs_with(picks_rows=[pick("20261017-01", GID, "moneyline", "COL", "", -110)])
    grading.apply(snapshots=ML_CLOSE, void_games={}, now=NOW, **tabs)
    tabs["picks"].rows[0]["placed"] = "yes"           # Max marks it placed afterwards
    rep = grading.apply(snapshots=ML_CLOSE, void_games={}, now=NOW, **tabs)
    assert [(r["group"], r["item_id"]) for r in rep.rows] == [("placed", "20261017-01")]


def test_bad_pick_row_is_skipped_not_fatal():
    tabs = tabs_with(picks_rows=[pick("20261017-01", GID, "moneyline", "XXX", "", -110),
                                 pick("20261017-02", GID, "moneyline", "COL", "", -110)])
    rep = grading.apply(snapshots=[], void_games={}, now=NOW, **tabs)
    assert rep.skipped == 1 and [r["item_id"] for r in rep.rows] == ["20261017-02"]


def test_columns_are_the_contract():
    assert PICKS_COLUMNS == ("pick_id", "pick_time_utc", "game_id", "market", "side", "line", "price",
                             "baseline_prob", "claude_prob", "claude_reason", "goalie_status", "label",
                             "ev", "stake_units", "placed", "actual_price",
                             "outside_prob", "outside_source")
    assert PARLAYS_COLUMNS == ("ticket_id", "ticket_time_utc", "leg_pick_ids", "combined_price",
                               "stake_units", "placed", "actual_price")
    assert PICK_GRADES_COLUMNS[:3] == ("group", "item_id", "label")
    assert {"clv", "clv_book", "close_prob", "result", "return_per_unit", "note",
            "prob_at_pick"} <= set(PICK_GRADES_COLUMNS)
