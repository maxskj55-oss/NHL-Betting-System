"""Step 3: Pinnacle no-vig baseline, EV, auto leans, stakes and the Tonight line rows.

Hand-worked numbers come from today's real pull (tests/fixtures/real/odds_20261017T153438Z.json):
Capitals (home) vs Devils moneyline, Pinnacle WSH -108 / NJD -102, FanDuel WSH -118 / NJD -102.

    implied WSH = 108/208 = 0.519231   implied NJD = 102/202 = 0.504950   sum = 1.024181
    no-vig  WSH = 0.519231/1.024181 = 0.506972      NJD = 0.493028
    fair    WSH = -100*0.506972/0.493028 = -102.83 -> -103      NJD = +103
    EV      WSH = 0.506972 * (1 + 100/118) - 1 = -0.063391
            NJD = 0.493028 * (1 + 100/102) - 1 = -0.023611
"""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from nhl_betting import tonight_lines as tl
from nhl_betting.config import ConfigError, _pricing, load_config
from nhl_betting.morning import (TONIGHT_COLUMNS, TONIGHT_PROTECTED, fixture_fetcher, memory_tabs)
from nhl_betting.morning import run as morning_run
from nhl_betting.pricing import (BET_CANDIDATE, LEAN, PASS, PASS_LINE_MISMATCH, PASS_NO_BASELINE,
                                 StakeBand, auto_lean, decimal_odds, expected_value, fair_american,
                                 implied_prob, no_vig, stake_units)
from nhl_betting.snapshots import build_rows
from nhl_betting.tabs import ProtectedColumnError, norm

REAL = Path(__file__).parent / "fixtures" / "real" / "odds_20261017T153438Z.json"
SNAP = datetime(2026, 10, 17, 15, 34, 38, tzinfo=timezone.utc)
TODAY = date(2026, 10, 17)
WSH_NJD = 2026020121      # 1 pm ET
MTL_BUF = 2026020126      # Over/Under 6.5 at both books
TBL_VAN = 2026020123      # FanDuel TBL -1.5, Pinnacle TBL -2.5


@pytest.fixture
def cfg():
    return load_config()


# ---- Math --------------------------------------------------------------------
def test_implied_and_no_vig_hand_worked():
    assert implied_prob(-110) == pytest.approx(110 / 210)
    assert implied_prob(150) == pytest.approx(0.4)
    assert no_vig([-110, -110]) == pytest.approx([0.5, 0.5])
    wsh, njd = no_vig([-108, -102])
    assert wsh == pytest.approx(0.506972, abs=1e-6)
    assert njd == pytest.approx(0.493028, abs=1e-6)
    # +150 / -170: 0.4 and 170/270 = 0.629630, sum 1.029630
    dog, fav = no_vig([150, -170])
    assert (dog, fav) == (pytest.approx(0.388489, abs=1e-6), pytest.approx(0.611511, abs=1e-6))
    assert dog + fav == pytest.approx(1)
    with pytest.raises(ValueError):
        implied_prob(-50)
    with pytest.raises(ValueError):
        no_vig([-110])


def test_fair_price_and_ev_hand_worked():
    assert fair_american(0.506972) == -103
    assert fair_american(0.493028) == 103
    assert fair_american(0.5) == -100
    assert fair_american(0.6) == -150 and fair_american(0.4) == 150
    assert decimal_odds(-118) == pytest.approx(1 + 100 / 118)
    assert decimal_odds(210) == pytest.approx(3.1)
    assert expected_value(0.506972, -118) == pytest.approx(-0.063391, abs=1e-6)
    assert expected_value(0.493028, -102) == pytest.approx(-0.023611, abs=1e-6)
    assert expected_value(0.5, 110) == pytest.approx(0.05)


def test_auto_lean_boundaries():
    assert auto_lean(0.03, 0.03) == BET_CANDIDATE
    assert auto_lean(0.0299, 0.03) == LEAN
    assert auto_lean(0.0001, 0.03) == LEAN
    assert auto_lean(0.0, 0.03) == PASS
    assert auto_lean(-0.05, 0.03) == PASS


def test_stake_bands(cfg):
    p = cfg.pricing
    units = [stake_units(ev, p.stake_bands, p.max_units)
             for ev in (None, -0.1, 0.0, 0.0299, 0.03, 0.0499, 0.05, 0.0799, 0.08, 0.25)]
    assert units == [0, 0, 0, 0, 0.5, 0.5, 0.75, 0.75, 1.0, 1.0]
    assert stake_units(0.5, (StakeBand(0.1, 2.0),), 1.5) == 1.5      # the cap


def test_pricing_config_is_validated():
    good = {"bet_candidate_min_ev": 0.03, "max_units": 1.5,
            "stake_bands": [{"min_ev": 0.08, "units": 1.0}, {"min_ev": 0.03, "units": 0.5}]}
    assert _pricing(good).bet_candidate_min_ev == 0.03
    for bad in ({"stake_bands": [{"min_ev": 0.02, "units": 0.5}]},          # a lean would get units
                {"stake_bands": [{"min_ev": 0.03, "units": 0.5}, {"min_ev": 0.08, "units": 1}]},
                {"bet_candidate_min_ev": 0}):
        with pytest.raises(ConfigError):
            _pricing({**good, **bad})


def test_formulas_use_config_and_contract_columns(cfg):
    ev = tl.adjusted_ev_formula()
    for ref in ("price,INDEX(M:M,r)", "cp,INDEX(R:R,r)", "cl,INDEX(V:V,r)", "ln,INDEX(L:L,r)",
                "bp,INDEX(N:N,r)"):
        assert ref in ev
    assert [TONIGHT_COLUMNS[i] for i in (11, 12, 13, 17, 21)] == \
        ["line", "fanduel_price", "baseline_prob", "claude_prob", "claude_line"]
    assert 'IF(AND(cp<>"",OR(cl="",cl=ln)),cp,bp)' in ev
    stake = tl.stake_units_formula(cfg.pricing)
    assert stake == ('=LET(r,ROW(),e,INDEX(T:T,r),IF(e="","",'
                     'MIN(1.5,IFS(e>=0.08,1,e>=0.05,0.75,e>=0.03,0.5,TRUE,0))))')
    other = replace(cfg.pricing, stake_bands=(StakeBand(0.1, 1.25),), max_units=1.0)
    assert "MIN(1,IFS(e>=0.1,1.25,TRUE,0))" in tl.stake_units_formula(other)


def test_adjusted_prob_follows_claude_line():
    row = {"claude_prob": 0.55, "baseline_prob": 0.5, "line": 6.5, "claude_line": ""}
    assert tl.adjusted_prob(row) == 0.55                       # line not moved yet
    assert tl.adjusted_prob(dict(row, claude_line=6.5)) == 0.55
    assert tl.adjusted_prob(dict(row, claude_line=6.0)) == 0.5  # moved: baseline
    assert tl.adjusted_prob(dict(row, claude_prob="")) == 0.5
    assert tl.adjusted_prob({"claude_prob": "", "baseline_prob": "", "line": ""}) is None


# ---- Tonight rows ------------------------------------------------------------
def snapshots(as_of=SNAP, change=None):
    odds = json.loads(REAL.read_text())
    if change:
        change(odds)
    rows, _ = build_rows(odds, as_of, ("fanduel", "pinnacle"), ("h2h", "spreads", "totals"))
    return rows


def morning_tonight(cfg, snaps):
    tabs = memory_tabs(cfg)
    morning_run(cfg, TODAY, fixture_fetcher(cfg.morning.fixtures_dir), tabs, snaps,
                datetime(2026, 10, 17, 12, 17, tzinfo=timezone.utc))
    return tabs.tonight


def price(cfg, tab, snaps, now=SNAP):
    return tl.apply(tab, snaps, cfg.pricing, now, cfg.morning.match_tolerance_hours)


def rows_of(tab, game_id=None):
    return {(int(r["game_id"]), r["market"], r["side"]): r for _, r in tab.read_rows()
            if r["market"] and (game_id is None or int(r["game_id"]) == game_id)}


def test_every_line_gets_a_baseline_or_a_pass_reason(cfg):
    snaps = snapshots()
    tab = morning_tonight(cfg, snaps)
    assert len(tab.rows) == 14 and all(not r["market"] for r in tab.rows)
    rep = price(cfg, tab, snaps)
    rows = rows_of(tab)
    assert len(rows) == 84 and len(tab.rows) == 84 and rep.placeholders_converted == 14
    for r in rows.values():
        if r["auto_lean"] in (PASS_LINE_MISMATCH, PASS_NO_BASELINE):
            assert (r["baseline_prob"], r["fair_price"], r["baseline_ev"]) == ("", "", "")
        else:
            assert 0 < r["baseline_prob"] < 1 and r["fair_price"] != "" and r["baseline_ev"] != ""
            assert r["auto_lean"] in (BET_CANDIDATE, LEAN, PASS)
        assert r["adjusted_ev"] == tl.adjusted_ev_formula()
        assert r["stake_units"] == tl.stake_units_formula(cfg.pricing)
    assert {m for (_, m, _) in rows} == {"moneyline", "puck_line", "total"}

    wsh, njd = rows[(WSH_NJD, "moneyline", "WSH")], rows[(WSH_NJD, "moneyline", "NJD")]
    assert (wsh["fanduel_price"], wsh["baseline_prob"], wsh["fair_price"]) == (-118, 0.506972, -103)
    assert wsh["baseline_ev"] == pytest.approx(-0.063391, abs=1e-6) and wsh["auto_lean"] == PASS
    assert (njd["baseline_prob"], njd["fair_price"]) == (0.493028, 103)
    assert njd["baseline_ev"] == pytest.approx(-0.023611, abs=1e-6)
    over = rows[(MTL_BUF, "total", "over")]
    assert (over["line"], over["fanduel_price"], over["baseline_prob"]) == (6.5, -110, 0.5)

    # Rows stay grouped: each game's six rows together, games by start time, markets in order.
    seq = [(r["start_time_utc"], int(r["game_id"]), r["market"]) for r in tab.rows]
    assert seq == sorted(seq)
    assert [r["market"] for r in tab.rows[:6]] == ["moneyline"] * 2 + ["puck_line"] * 2 + ["total"] * 2


def test_line_mismatch_is_a_pass_not_a_guess(cfg):
    snaps = snapshots()
    tab = morning_tonight(cfg, snaps)
    price(cfg, tab, snaps)
    rows = rows_of(tab)
    mismatched = {k for k, r in rows.items() if r["auto_lean"] == PASS_LINE_MISMATCH}
    assert len(mismatched) == 18                       # 2 puck lines + 7 totals, both sides
    # Pinnacle hangs TBL -2.5, FanDuel -1.5.
    assert {(TBL_VAN, "puck_line", "TBL"), (TBL_VAN, "puck_line", "VAN")} <= mismatched
    assert rows[(TBL_VAN, "puck_line", "TBL")]["line"] == -1.5
    # Same number, other team: FanDuel WSH -1.5 vs Pinnacle WSH +1.5.
    assert {(WSH_NJD, "puck_line", "WSH"), (WSH_NJD, "puck_line", "NJD")} <= mismatched
    # FanDuel 6.5 vs Pinnacle 6.0.
    assert {(WSH_NJD, "total", "over"), (WSH_NJD, "total", "under")} <= mismatched
    assert rows[(MTL_BUF, "total", "over")]["auto_lean"] == PASS   # same 6.5: priced


def test_missing_pinnacle_is_no_baseline(cfg):
    def drop_pinnacle(odds):
        for ev in odds:
            if ev["away_team"] == "Buffalo Sabres":
                ev["bookmakers"] = [b for b in ev["bookmakers"] if b["key"] != "pinnacle"]
            if ev["home_team"] == "Washington Capitals":
                for b in ev["bookmakers"]:
                    if b["key"] == "pinnacle":
                        b["markets"] = [m for m in b["markets"] if m["key"] != "h2h"]
    snaps = snapshots(change=drop_pinnacle)
    tab = morning_tonight(cfg, snaps)
    price(cfg, tab, snaps)
    mtl = rows_of(tab, MTL_BUF)
    assert len(mtl) == 6 and all(r["auto_lean"] == PASS_NO_BASELINE and r["baseline_prob"] == ""
                                 for r in mtl.values())
    wsh = rows_of(tab, WSH_NJD)
    assert {r["auto_lean"] for (_, m, _), r in wsh.items() if m == "moneyline"} == {PASS_NO_BASELINE}
    assert len(rows_of(tab)) == 84


def test_protected_columns_survive_a_re_pull(cfg, caplog):
    snaps1 = snapshots()
    tab = morning_tonight(cfg, snaps1)
    price(cfg, tab, snaps1)
    # Claude writes on three rows (through the Sheets connector, not code).
    claude = {(MTL_BUF, "total", "over"): (0.55, "Over: both starters out"),
              (MTL_BUF, "puck_line", "BUF"): (0.70, "Backup in net"),
              (MTL_BUF, "moneyline", "BUF"): (0.47, "Travel spot")}
    for r in tab.rows:
        k = (int(r["game_id"]), r["market"], r["side"])
        if k in claude:
            r["claude_prob"], r["claude_reason"] = claude[k]
    tab.updated.clear()

    # Later pull: FanDuel and Pinnacle move the MTL-BUF total to 6.0 and FanDuel's prices move.
    def move(odds):
        for ev in odds:
            if ev["away_team"] == "Buffalo Sabres":
                for b in ev["bookmakers"]:
                    for m in b["markets"]:
                        if m["key"] == "totals":
                            for o in m["outcomes"]:
                                o["point"] = 6.0
                        if b["key"] == "fanduel" and m["key"] == "h2h":
                            for o in m["outcomes"]:
                                o["price"] = 125 if o["name"] == "Buffalo Sabres" else -150
    later = SNAP + timedelta(hours=2)
    snaps2 = snaps1 + snapshots(as_of=later, change=move)
    with caplog.at_level("WARNING"):
        rep = price(cfg, tab, snaps2, now=later)

    rows = rows_of(tab, MTL_BUF)
    for k, (cp, why) in claude.items():
        assert (rows[k]["claude_prob"], rows[k]["claude_reason"]) == (cp, why)
    for _, cells in tab.updated:
        assert not set(cells) & set(TONIGHT_PROTECTED)
    assert rep.added == 0 and len(tab.rows) == 84

    over = rows[(MTL_BUF, "total", "over")]
    assert (over["line"], over["claude_line"]) == (6.0, 6.5)       # Claude priced 6.5
    assert tl.adjusted_prob(over) == over["baseline_prob"]           # so the baseline is used
    assert "line moved 6.5 -> 6" in caplog.text
    pl = rows[(MTL_BUF, "puck_line", "BUF")]
    assert pl["claude_line"] == 1.5 and tl.adjusted_prob(pl) == 0.70
    ml = rows[(MTL_BUF, "moneyline", "BUF")]
    assert ml["claude_line"] == "" and ml["fanduel_price"] == 125    # no line on a moneyline
    assert tl.adjusted_prob(ml) == 0.47
    assert ml["adjusted_ev"] == tl.adjusted_ev_formula()             # formulas rewritten, not values
    assert rep.claude_lines_recorded == 2

    # A third pull leaves claude_line alone (recorded once).
    price(cfg, tab, snaps2 + snapshots(as_of=later + timedelta(minutes=30), change=move),
          now=later + timedelta(minutes=30))
    assert rows_of(tab, MTL_BUF)[(MTL_BUF, "total", "over")]["claude_line"] == 6.5


def test_game_row_with_claude_notes_is_left_alone(cfg, caplog):
    snaps = snapshots()
    tab = morning_tonight(cfg, snaps)
    for r in tab.rows:
        if int(r["game_id"]) == MTL_BUF:
            r["claude_reason"] = "Watch the goalie news"
    price(cfg, tab, snaps)
    mtl = [r for r in tab.rows if int(r["game_id"]) == MTL_BUF]
    assert len(mtl) == 7 and sum(1 for r in mtl if not r["market"]) == 1
    assert "Claude's notes" in caplog.text


def test_started_games_and_late_snapshots_are_ignored(cfg):
    snaps = snapshots()
    tab = morning_tonight(cfg, snaps)
    now = datetime(2026, 10, 17, 17, 30, tzinfo=timezone.utc)       # WSH-NJD started at 17:00
    price(cfg, tab, snaps, now=now)
    wsh = [r for r in tab.rows if int(r["game_id"]) == WSH_NJD]
    assert len(wsh) == 1 and wsh[0]["market"] == ""                 # untouched game row
    assert len(rows_of(tab)) == 78

    # A snapshot stamped at or after puck drop is never used.
    late = snapshots(as_of=datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc))
    for r in late:
        r["snapshot_time_utc"], r["price_american"] = "2026-10-18T03:00:00Z", 999
    latest = tl.latest_pre_start(snaps + late)
    assert all(r["snapshot_time_utc"] == "2026-10-17T15:34:38Z"
               for rows in latest.values() for r in rows)


def test_formula_columns_take_formulas_only(cfg):
    snaps = snapshots()
    tab = morning_tonight(cfg, snaps)
    price(cfg, tab, snaps)
    with pytest.raises(ProtectedColumnError):
        tab.update_cells([(2, {"adjusted_ev": 0.04})])
    with pytest.raises(ProtectedColumnError):
        tab.update_cells([(2, {"stake_units": "0.5"})])
    tab.update_cells([(2, {"stake_units": "=1"})])


def test_values_read_back_from_a_sheet_still_match(cfg):
    """Live reads return numbers (line 6.5, game_id 2026020126); keys must still match."""
    snaps = snapshots()
    tab = morning_tonight(cfg, snaps)
    price(cfg, tab, snaps)
    for r in tab.rows:
        r["game_id"] = float(r["game_id"])
        for c in ("adjusted_ev", "stake_units"):
            r[c] = -0.05                     # a sheet returns the computed value
    for s in snaps:
        s["line"] = float(s["line"]) if s["line"] != "" else ""
    rep = price(cfg, tab, snaps)
    assert rep.added == 0 and rep.updated == 0 and len(tab.rows) == 84
    assert all(norm(r["adjusted_ev"]).startswith("=") for r in tab.rows)
