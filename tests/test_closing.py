"""Closing job: start-time groups, the credit rule, skipped groups, the 5.5-hour hand-off."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from nhl_betting import closing
from nhl_betting.closing import Env, SimClock, start_groups
from nhl_betting.config import load_config
from nhl_betting.morning import TONIGHT_COLUMNS
from nhl_betting.odds_api import BudgetError, Usage
from nhl_betting.tabs import MemoryTab

T0 = datetime(2026, 10, 17, 17, 0, tzinfo=timezone.utc)      # run starts 1 pm ET


@pytest.fixture
def cfg():
    return load_config()


def tonight_row(gid, start, lean="pass", market="moneyline", side="AAA"):
    return {"game_id": gid, "start_time_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "home": "HOM", "away": "AWY", "market": market, "side": side, "auto_lean": lean}


class Fake:
    """Env pieces that record what happened."""

    def __init__(self, tonight, picks=None, start=T0):
        self.clock = SimClock(start)
        self.tonight = MemoryTab("Tonight", TONIGHT_COLUMNS, ("claude_prob", "claude_reason"),
                                 rows=tonight)
        self.picks = picks or (lambda now: [])
        self.pulls: list[datetime] = []
        self.dispatches: list[tuple[int, str]] = []
        self.slept: list[float] = []

    def env(self):
        def sleep(s):
            self.slept.append(s)
            self.clock.sleep(s)

        def pull(now):
            self.pulls.append(now)
            return 3

        return Env(clock=self.clock, sleep=sleep,
                   read_tonight=lambda: [r for _, r in self.tonight.read_rows()],
                   read_picks=lambda: self.picks(self.clock()),
                   pull=pull, dispatch=lambda h, a: self.dispatches.append((h, a)))


def test_groups_by_start_time_and_skips_started_games():
    rows = [tonight_row(1, T0 + timedelta(hours=2)), tonight_row(1, T0 + timedelta(hours=2)),
            tonight_row(2, T0 + timedelta(hours=2)), tonight_row(3, T0 + timedelta(hours=3)),
            tonight_row(4, T0 - timedelta(minutes=5))]
    groups = start_groups(rows, T0)
    assert [g.game_ids for g in groups] == [("1", "2"), ("3",)]
    assert start_groups(rows, T0, after=T0 + timedelta(hours=2))[0].game_ids == ("3",)


def test_pulls_15_minutes_before_groups_with_picks_or_leans(cfg):
    a, b, c = (T0 + timedelta(hours=h) for h in (1, 2, 3))
    fake = Fake([tonight_row(1, a, lean="lean"), tonight_row(2, b),
                 tonight_row(3, c, lean="bet candidate")],
                picks=lambda now: [{"pick_id": "20261017-01", "game_id": "2"}])
    rep = closing.run(cfg, fake.env(), started_at=T0)
    assert fake.pulls == [a - timedelta(minutes=15), b - timedelta(minutes=15), c - timedelta(minutes=15)]
    assert rep.credits == 9 and rep.skipped == [] and not rep.handed_off


def test_skips_empty_groups_logs_them_and_spends_nothing(cfg, caplog):
    caplog.set_level(logging.INFO, logger="nhl_betting")
    a, b = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    fake = Fake([tonight_row(1, a), tonight_row(2, b, lean="pass: line mismatch")])
    rep = closing.run(cfg, fake.env(), started_at=T0)
    assert fake.pulls == [] and rep.credits == 0 and len(rep.skipped) == 2
    assert sum("Skipped group" in m and "0 credits" in m for m in caplog.messages) == 2


def test_picks_written_after_the_run_starts_still_count(cfg):
    a = T0 + timedelta(hours=3)
    # No picks at 1 pm; the pick appears at 4:30 pm ET (picker runs 60-90 min before puck drop).
    fake = Fake([tonight_row(7, a)],
                picks=lambda now: [{"game_id": "7"}] if now >= T0 + timedelta(hours=2) else [])
    rep = closing.run(cfg, fake.env(), started_at=T0)
    assert fake.pulls == [a - timedelta(minutes=15)] and rep.credits == 3


def test_hands_off_when_the_next_wake_is_past_5_5_hours(cfg, caplog):
    a, late = T0 + timedelta(hours=1), T0 + timedelta(hours=6)
    fake = Fake([tonight_row(1, a, lean="lean"), tonight_row(2, late, lean="lean")])
    rep = closing.run(cfg, fake.env(), started_at=T0)
    assert fake.pulls == [a - timedelta(minutes=15)]           # only the first group
    assert fake.dispatches == [(1, "2026-10-17T18:00:00Z")]    # fresh run covers groups after 2 pm ET
    assert rep.handed_off
    # It waited until 330 minutes into the run before handing off (5:45 > 5:30).
    assert fake.clock() == T0 + timedelta(minutes=330)


def test_fresh_run_covers_only_later_groups(cfg):
    a, late = T0 + timedelta(hours=1), T0 + timedelta(hours=6)
    start = T0 + timedelta(minutes=331)
    fake = Fake([tonight_row(1, a, lean="lean"), tonight_row(2, late, lean="lean")], start=start)
    rep = closing.run(cfg, fake.env(), hop=1, after=a, started_at=start)
    assert fake.pulls == [late - timedelta(minutes=15)] and not rep.handed_off


def test_group_not_started_but_handled_earlier_is_not_pulled_twice(cfg):
    # Run 1 pulled the 6:00 group at 5:45 and handed off at 6:30 limit... the fresh run
    # starts before 6:00's puck drop could be past; `after` keeps it from pulling again.
    g = T0 + timedelta(hours=5, minutes=40)
    start = T0 + timedelta(minutes=331)
    fake = Fake([tonight_row(1, g, lean="lean")], start=start)
    closing.run(cfg, fake.env(), hop=1, after=g, started_at=start)
    assert fake.pulls == []


def test_hand_off_limit_stops_the_chain(cfg, caplog):
    late = T0 + timedelta(hours=7)
    fake = Fake([tonight_row(1, late, lean="lean")])
    rep = closing.run(cfg, fake.env(), hop=cfg.closing.max_handoffs, started_at=T0)
    assert fake.dispatches == [] and fake.pulls == [] and len(rep.not_covered) == 1
    assert any("Hand-off limit" in m for m in caplog.messages)


def test_wake_time_already_past_pulls_right_away(cfg):
    a = T0 + timedelta(minutes=10)                  # wake time (12:55 pm) has passed; puck drop hasn't
    fake = Fake([tonight_row(1, a, lean="lean")])
    rep = closing.run(cfg, fake.env(), started_at=T0)
    assert fake.pulls == [T0] and rep.missed == []


def test_group_started_by_the_time_the_run_wakes_is_missed(cfg, caplog):
    a = T0 + timedelta(hours=1)
    fake = Fake([tonight_row(1, a, lean="lean")])
    env = fake.env()
    env.sleep = lambda s: fake.clock.sleep(s + 20 * 60)     # woke 20 minutes late
    rep = closing.run(cfg, env, started_at=T0)
    assert fake.pulls == [] and len(rep.missed) == 1
    assert any(m.startswith("Missed") for m in caplog.messages)


def test_never_writes_tonight(cfg):
    a = T0 + timedelta(hours=1)
    fake = Fake([tonight_row(1, a, lean="lean")])
    closing.run(cfg, fake.env(), started_at=T0)
    assert fake.tonight.updated == [] and fake.tonight.appended == [] and fake.tonight.deleted == []


# ---- The live pull and hand-off wiring (fakes, no network) --------------------
class FakeClient:
    def __init__(self, used, odds):
        self.used, self.odds, self.calls = used, odds, []

    def get_events(self, start, end):
        self.calls.append("events")
        return [{"id": "e", "commence_time": "2026-10-17T23:10:00Z", "home_team": "Detroit Red Wings",
                 "away_team": "San Jose Sharks"}], Usage(self.used, 500 - self.used, 0)

    def get_odds(self, start, end):
        self.calls.append("odds")
        return self.odds, Usage(self.used + 3, 497 - self.used, 3)


class ListSink:
    def __init__(self):
        self.rows = []

    def append(self, rows):
        self.rows.extend(rows)
        return len(rows)


def test_live_pull_checks_budget_before_paying(cfg, tmp_path):
    import dataclasses
    cfg = dataclasses.replace(cfg, raw_dir=tmp_path / "raw")
    client = FakeClient(used=298, odds=[])
    pull = closing.make_pull(cfg, client, [ListSink()], lambda: T0)
    with pytest.raises(BudgetError):
        pull(T0)
    assert client.calls == ["events"]


def test_live_pull_appends_snapshot_rows(cfg, tmp_path):
    import dataclasses
    import json
    cfg = dataclasses.replace(cfg, raw_dir=tmp_path / "raw")
    odds = json.loads(sorted((cfg.odds_fixture.parent / "real").glob("odds_*.json"))[-1].read_text())
    sink = ListSink()
    snap = datetime(2026, 10, 17, 22, 50, tzinfo=timezone.utc)
    spent = closing.make_pull(cfg, FakeClient(used=20, odds=odds), [sink], lambda: snap)(snap)
    assert spent == 3 and sink.rows
    assert {r["snapshot_time_utc"] for r in sink.rows} == {"2026-10-17T22:50:00Z"}
    assert all(r["start_time_utc"] > "2026-10-17T22:50:00Z" for r in sink.rows)   # started games dropped


def test_github_dispatch_payload(monkeypatch):
    sent = {}

    class Resp:
        status_code, text = 204, ""

    class Session:
        def post(self, url, json, headers, timeout):
            sent.update(url=url, json=json, auth=headers["Authorization"])
            return Resp()

    monkeypatch.setenv("GITHUB_REPOSITORY", "maxskj55-oss/nhl-betting-system")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    closing.github_dispatch(1, "2026-10-17T18:00:00Z", session=Session())
    assert sent["url"].endswith("/repos/maxskj55-oss/nhl-betting-system/actions/workflows/closing.yml/dispatches")
    assert sent["json"] == {"ref": "main", "inputs": {"hop": "1", "after": "2026-10-17T18:00:00Z"}}
    assert sent["auth"] == "Bearer t0ken"


def test_github_dispatch_failure_is_an_error(monkeypatch):
    class Resp:
        status_code, text = 403, "Resource not accessible by integration"

    class Session:
        def post(self, *a, **k):
            return Resp()

    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    with pytest.raises(closing.HandoffError, match="Run workflow by hand"):
        closing.github_dispatch(1, "", session=Session())


def test_dry_run_hands_off_on_the_long_saturday_fixture(cfg):
    # Oct 17 slate: 1:00 pm to 10:00 pm ET, longer than one run's 5.5 hours.
    rep = closing.run_dry(cfg, 0, None)
    assert rep.handed_off and rep.credits == 0 and len(rep.skipped) == 3
