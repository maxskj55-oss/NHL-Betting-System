"""Morning job end to end on saved NHL fixtures, with in-memory tabs (no network)."""

import dataclasses
from datetime import date, datetime, timezone

import pytest

from nhl_betting import morning
from nhl_betting.config import load_config
from nhl_betting.morning import (ARCHIVE_COLUMNS, GAME_MAP_COLUMNS, RESULTS_COLUMNS,
                                 TONIGHT_COLUMNS, TONIGHT_OWNED, TONIGHT_PROTECTED, MorningTabs,
                                 fixture_fetcher, memory_tabs)
from nhl_betting.tabs import MemoryTab, ProtectedColumnError

TODAY = date(2026, 10, 17)
NOW = datetime(2026, 10, 17, 12, 17, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load_config()


def snapshot(event, book, side, line, snap="2026-10-15T20:00:00Z",
             home="Nashville Predators", away="San Jose Sharks", start="2026-10-16T00:10:00Z"):
    return {"snapshot_time_utc": snap, "odds_event_id": event, "start_time_utc": start,
            "home_team": home, "away_team": away, "book": book, "market": "totals",
            "side": side, "line": line, "price_american": -110, "price_decimal": 1.9091,
            "book_last_update_utc": snap, "pre_start_check": True}


def run(cfg, tabs, snapshots=(), today=TODAY, fetch=None):
    return morning.run(cfg, today, fetch or fixture_fetcher(cfg.morning.fixtures_dir), tabs,
                       list(snapshots), NOW)


def by_id(tab):
    return {int(r["game_id"]): r for _, r in tab.read_rows()}


def test_columns_are_the_contract():
    assert TONIGHT_COLUMNS[:8] == TONIGHT_OWNED
    assert {"claude_prob", "claude_reason", "adjusted_ev", "projected_goalies",
            "baseline_prob", "fanduel_price", "stake_units"} <= set(TONIGHT_COLUMNS)
    # Step 3 added claude_line at the end; earlier columns keep their order.
    assert TONIGHT_COLUMNS[-1] == "claude_line" and TONIGHT_COLUMNS.index("stake_units") == 20
    assert ARCHIVE_COLUMNS == TONIGHT_COLUMNS[:-1] + ("archived_at_utc", "claude_line")
    assert set(TONIGHT_PROTECTED) == {"claude_prob", "claude_reason"}
    assert set(morning.TONIGHT_FORMULAS) == {"adjusted_ev", "stake_units"}
    assert {"ot_so", "ml_winner", "pl_home_-1.5", "pl_away_-1.5", "total_goals",
            "closing_fanduel", "closing_pinnacle", "clv"} <= set(RESULTS_COLUMNS)
    assert GAME_MAP_COLUMNS[:2] == ("odds_event_id", "game_id")


def test_first_run_lists_every_game_and_yesterdays_finals(cfg):
    tabs = memory_tabs(cfg)
    rep = run(cfg, tabs)
    tonight = by_id(tabs.tonight)
    assert len(tonight) == 14 and rep.tonight_added == 14
    assert all(r["game_date_et"] == "2026-10-17" for r in tonight.values())
    # BOS and PIT played on Oct 16; nobody else on today's slate did.
    b2b = {r["away"] for r in tonight.values() if r["away_b2b"]} | \
          {r["home"] for r in tonight.values() if r["home_b2b"]}
    assert b2b == {"BOS", "PIT"}
    assert tonight[2026020121]["start_time_et"] == "2026-10-17 13:00"
    for r in tonight.values():   # odds, Claude and formula columns stay empty
        assert all(r[c] == "" for c in TONIGHT_COLUMNS if c not in TONIGHT_OWNED)

    results = by_id(tabs.results)
    assert {2026020119, 2026020120} <= set(results)       # yesterday's finals
    assert len(results) == 15                             # Oct 14-16 (3-day lookback)
    so = results[2026020115]                              # SJS 3 @ NSH 2, shootout
    assert (so["ot_so"], so["ml_winner"], so["total_goals"]) == ("SO", "SJS", 5)
    assert (so["pl_home_-1.5"], so["pl_away_-1.5"]) == ("loss", "loss")
    assert results[2026020108]["ot_so"] == "OT"
    assert results[2026020113]["pl_home_-1.5"] == "win"  # NJD 7-1
    assert all(r[c] == "" for r in results.values()
               for c in ("closing_fanduel", "closing_pinnacle", "clv"))
    assert rep.problems == []


def test_rerun_is_a_no_op(cfg):
    tabs = memory_tabs(cfg)
    run(cfg, tabs)
    rep = run(cfg, tabs)
    assert (rep.tonight_added, rep.tonight_updated, rep.settled, rep.mapped, rep.archived) == (0, 0, 0, 0, 0)
    assert len(tabs.tonight.rows) == 14 and len(tabs.results.rows) == 15


def test_totals_settle_against_the_last_snapshot_line_including_a_whole_number_push(cfg):
    tabs = memory_tabs(cfg)
    snaps = [
        snapshot("ev115", "fanduel", "Over", 5.0),
        snapshot("ev115", "fanduel", "Under", 5.0),
        snapshot("ev115", "pinnacle", "Over", 6.5, snap="2026-10-15T15:00:00Z"),
        snapshot("ev115", "pinnacle", "Over", 5.5, snap="2026-10-15T21:00:00Z"),   # latest wins
    ]
    rep = run(cfg, tabs, snaps)
    assert rep.mapped == 1
    gm = tabs.game_map.read_rows()[0][1]
    assert (gm["odds_event_id"], gm["game_id"]) == ("ev115", 2026020115)
    so = by_id(tabs.results)[2026020115]
    assert (so["total_line_fanduel"], so["total_result_fanduel"]) == (5.0, "push")
    assert (so["total_line_pinnacle"], so["total_result_pinnacle"]) == (5.5, "under")
    other = by_id(tabs.results)[2026020119]
    assert other["total_line_fanduel"] == "" and other["total_result_fanduel"] == ""


def test_claude_cells_survive_archive_and_refresh(cfg):
    tabs = memory_tabs(cfg)
    old = {c: "" for c in TONIGHT_COLUMNS}
    old.update(game_id=2026020119, game_date_et="2026-10-16", home="COL", away="PIT",
               claude_prob=0.61, claude_reason="Starter confirmed")
    today_row = {c: "" for c in TONIGHT_COLUMNS}
    today_row.update(game_id=2026020121, game_date_et="2026-10-17", start_time_utc="2026-10-17T18:00:00Z",
                     home="WSH", away="NJD", claude_prob=0.55, claude_reason="keep me",
                     adjusted_ev="=formula")
    tabs.tonight = MemoryTab("Tonight", TONIGHT_COLUMNS, TONIGHT_PROTECTED, [old, today_row])

    rep = run(cfg, tabs)
    assert rep.archived == 1
    archived = tabs.archive.read_rows()[0][1]
    assert (archived["claude_prob"], archived["claude_reason"]) == (0.61, "Starter confirmed")
    assert archived["archived_at_utc"] == "2026-10-17T12:17:00Z"
    tonight = by_id(tabs.tonight)
    assert 2026020119 not in tonight and len(tonight) == 14
    wsh = tonight[2026020121]
    assert (wsh["claude_prob"], wsh["claude_reason"], wsh["adjusted_ev"]) == (0.55, "keep me", "=formula")
    assert wsh["start_time_utc"] == "2026-10-17T17:00:00Z"          # owned cell refreshed
    for _, cells in tabs.tonight.updated:
        assert not set(cells) & set(TONIGHT_PROTECTED)

    # Archiving twice (e.g. a crash between archive and delete) doesn't duplicate.
    tabs.tonight.rows.append(dict(old))
    run(cfg, tabs)
    assert len(tabs.archive.rows) == 1


def test_step3_style_rows_get_owned_cells_refreshed_per_game(cfg):
    tabs = memory_tabs(cfg)
    rows = []
    for side in ("Washington Capitals", "New Jersey Devils"):
        r = {c: "" for c in TONIGHT_COLUMNS}
        r.update(game_id=2026020121, game_date_et="2026-10-17", market="h2h", side=side,
                 claude_prob=0.5)
        rows.append(r)
    tabs.tonight = MemoryTab("Tonight", TONIGHT_COLUMNS, TONIGHT_PROTECTED, rows)
    run(cfg, tabs)
    wsh_rows = [r for _, r in tabs.tonight.read_rows() if r["game_id"] == 2026020121]
    assert len(wsh_rows) == 2 and all(r["home"] == "WSH" and r["claude_prob"] == 0.5 for r in wsh_rows)


def test_protected_columns_are_refused():
    tab = MemoryTab("Tonight", TONIGHT_COLUMNS, TONIGHT_PROTECTED, [{"game_id": 1}])
    with pytest.raises(ProtectedColumnError):
        tab.update_cells([(2, {"claude_prob": 0.5})])
    with pytest.raises(ProtectedColumnError):
        tab.append_rows([{"game_id": 2, "claude_reason": "x"}], ("game_id",))
    res = MemoryTab("Results", RESULTS_COLUMNS, morning.RESULTS_PROTECTED)
    with pytest.raises(ProtectedColumnError):
        res.append_rows([{"game_id": 2, "clv": 0.02}], ("game_id",))


def _patched_fetch(cfg, change):
    base = fixture_fetcher(cfg.morning.fixtures_dir)

    def fetch(d):
        return [change(g) for g in base(d)]
    return fetch


def test_unsettleable_score_is_reported_and_others_still_written(cfg):
    tabs = memory_tabs(cfg)

    def tie(g):
        return dataclasses.replace(g, home_score=3, away_score=3) if g.game_id == 2026020115 else g
    rep = run(cfg, tabs, fetch=_patched_fetch(cfg, tie))
    assert len(rep.problems) == 1 and "2026020115" in rep.problems[0]
    assert 2026020115 not in by_id(tabs.results) and len(by_id(tabs.results)) == 14


def test_unfinished_and_postponed_games(cfg, caplog):
    tabs = memory_tabs(cfg)

    def change(g):
        if g.game_id == 2026020120:   # yesterday's late game still live at run time
            return dataclasses.replace(g, state="LIVE", home_score=None, away_score=None, last_period=None)
        if g.game_id == 2026020134:   # tonight's PIT @ VGK postponed
            return dataclasses.replace(g, schedule_state="PPD")
        return g
    run(cfg, tabs, fetch=_patched_fetch(cfg, change))
    assert 2026020120 not in by_id(tabs.results)
    assert 2026020134 not in by_id(tabs.tonight) and len(by_id(tabs.tonight)) == 13
    assert "not final yet" in caplog.text and "PPD" in caplog.text
    # Next run, once it's final, it settles.
    run(cfg, tabs)
    assert 2026020120 in by_id(tabs.results)


def test_dry_run_cli(capsys):
    assert morning.main(["--dry-run"]) == 0


def test_morning_never_touches_the_odds_api(cfg, monkeypatch):
    import nhl_betting.odds_api as odds_api

    def boom(*a, **k):
        raise AssertionError("morning job must not call the Odds API")
    monkeypatch.setattr(odds_api.OddsApiClient, "_get", boom)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    run(cfg, memory_tabs(cfg))


# ---- Step 4: the morning job grades yesterday's picks ------------------------
def test_morning_job_grades_a_test_pick_automatically(cfg):
    """Oct 16: PIT @ COL finished 5-2. A TEST- pick on COL is graded on the Oct 17 run."""
    def ml(book, team, price):
        return dict(snapshot("evCOL", book, team, "", snap="2026-10-17T00:45:00Z",
                             home="Colorado Avalanche", away="Pittsburgh Penguins",
                             start="2026-10-17T01:08:00Z"), market="h2h", price_american=price)
    snaps = [ml("pinnacle", "Colorado Avalanche", -200), ml("pinnacle", "Pittsburgh Penguins", 170),
             ml("fanduel", "Colorado Avalanche", -190), ml("fanduel", "Pittsburgh Penguins", 160)]
    tabs = memory_tabs(cfg)
    tabs.picks.rows.append({c: "" for c in tabs.picks.columns} | {
        "pick_id": "TEST-20261016-01", "pick_time_utc": "2026-10-16T23:30:00Z",
        "game_id": 2026020119, "market": "moneyline", "side": "COL", "line": "", "price": -190,
        "baseline_prob": 0.63, "claude_prob": 0.64, "label": "lean", "stake_units": 0,
        "placed": "no"})
    rep = run(cfg, tabs, snaps)
    grades = tabs.pick_grades.appended
    assert rep.graded == 1 and len(grades) == 1
    g = grades[0]
    assert (g["group"], g["item_id"], g["result"], g["note"]) == ("claude", "TEST-20261016-01", "win", "test")
    # Pinnacle -200 / +170 -> COL no-vig 9/14; -190 is decimal 29/19; CLV = 261/266 - 1 = -5/266.
    assert g["close_prob"] == round(9 / 14, 6) and g["clv_book"] == "pinnacle"
    assert g["clv"] == round(-5 / 266, 6)
    assert (g["close_pinnacle_price"], g["close_fanduel_price"]) == (-200, -190)
    assert g["prob_at_pick"] == 0.64 and g["game_date_et"] == "2026-10-16"
    assert tabs.picks.updated == [] and tabs.picks.appended == []
    # Results' step-4 columns stay blank (superseded by Pick_Grades).
    col = by_id(tabs.results)[2026020119]
    assert (col["closing_fanduel"], col["closing_pinnacle"], col["clv"]) == ("", "", "")
    # A second morning run adds nothing.
    assert run(cfg, tabs, snaps).graded == 0
