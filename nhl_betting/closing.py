"""Evening closing snapshots: one manually started run that waits for each start-time group.

    python -m nhl_betting.closing                 # live
    python -m nhl_betting.closing --dry-run       # saved fixtures, simulated clock, no API calls

Games in Tonight are grouped by start time. For each group the run sleeps until
`closing.minutes_before_start` (15) minutes before puck drop, then:

- re-reads Picks and Tonight (picks are written 60-90 minutes before puck drop,
  so this is checked at wake-up, not at the start of the run);
- skips the group, with a log line, unless it has a Picks row or a Tonight row
  whose auto_lean is "bet candidate" or "lean";
- otherwise makes the free /events call, checks the monthly budget, and pulls
  every game not yet started (3 credits: the same cost as one group) into
  Odds_Snapshots. Rows for started games are dropped as usual.

A game's closing price is its last pre-start snapshot; the morning job grades
from it (grading.py). This job never writes Tonight, Picks or Parlays.

GitHub stops a job at 6 hours. If the next wake-up is more than
`closing.handoff_after_minutes` (330) after this run started, the run waits
until that limit, starts a fresh run of this workflow for the remaining groups
(workflow_dispatch) and exits. The fresh run
only handles groups starting after the last one this run handled, and the
chain stops after `closing.max_handoffs` fresh runs.

Exit codes: 0 ok, 1 error (including a failed hand-off), 2 over budget, 3 API shape changed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

import requests

from .config import Config, ConfigError, get_odds_api_key, load_config
from .odds_api import BudgetError, OddsApiClient, OddsApiError, check_budget, iso_z
from .pull_odds import slate_window
from .sheets import SheetsError, SheetsSink, authorized_session, get_sheet_id, read_tab
from .sinks import CsvSink, Sink
from .snapshots import COLUMNS, SchemaError, build_rows, parse_time, validate_events
from .tabs import norm

log = logging.getLogger("nhl_betting.closing")

WORKFLOW_FILE = "closing.yml"


class HandoffError(RuntimeError):
    """Starting the fresh run failed."""


@dataclass(frozen=True)
class Group:
    start_utc: datetime
    game_ids: tuple[str, ...]
    matchups: tuple[str, ...]

    def label(self, tz_name: str) -> str:
        from zoneinfo import ZoneInfo
        local = self.start_utc.astimezone(ZoneInfo(tz_name)).strftime("%-I:%M %p ET")
        return f"{local} ({iso_z(self.start_utc)}: {', '.join(self.matchups)})"


def start_groups(tonight_rows: Sequence[dict], now: datetime,
                 after: datetime | None = None) -> list[Group]:
    """Games in Tonight not started yet (and starting after `after`), grouped by start time."""
    games: dict[str, tuple[datetime, str]] = {}
    for r in tonight_rows:
        gid = norm(r.get("game_id"))
        if not gid or gid in games:
            continue
        try:
            start = parse_time(norm(r.get("start_time_utc")))
        except ValueError:
            log.warning("Tonight: game %s has no readable start_time_utc; not covered", gid)
            continue
        games[gid] = (start, f"{norm(r.get('away'))} @ {norm(r.get('home'))}")
    by_start: dict[datetime, list[tuple[str, str]]] = {}
    for gid, (start, matchup) in games.items():
        if start <= now or (after is not None and start <= after):
            continue
        by_start.setdefault(start, []).append((gid, matchup))
    return [Group(s, tuple(g for g, _ in sorted(v)), tuple(m for _, m in sorted(v)))
            for s, v in sorted(by_start.items())]


def qualifies(group: Group, picks_rows: Sequence[dict], tonight_rows: Sequence[dict],
              leans: Sequence[str]) -> str:
    """Why this group is worth credits ("" = it isn't): Picks rows or qualifying auto leans."""
    ids = set(group.game_ids)
    picks = sum(1 for r in picks_rows if norm(r.get("game_id")) in ids)
    lean_rows = sum(1 for r in tonight_rows
                    if norm(r.get("game_id")) in ids and norm(r.get("auto_lean")) in leans)
    parts = []
    if picks:
        parts.append(f"{picks} pick(s)")
    if lean_rows:
        parts.append(f"{lean_rows} Tonight row(s) marked {' / '.join(leans)}")
    return ", ".join(parts)


@dataclass
class Env:
    """Everything the run touches, so tests and the dry run can swap it out."""
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    read_tonight: Callable[[], list[dict]]
    read_picks: Callable[[], list[dict]]
    pull: Callable[[datetime], int]              # returns credits spent
    dispatch: Callable[[int, str], None]         # (hop, after ISO time) -> start a fresh run


@dataclass
class ClosingReport:
    pulled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    not_covered: list[str] = field(default_factory=list)
    handed_off: bool = False
    credits: int = 0


def run(cfg: Config, env: Env, hop: int = 0, after: datetime | None = None,
        started_at: datetime | None = None) -> ClosingReport:
    c = cfg.closing
    tz = cfg.slate_timezone
    t0 = started_at or env.clock()
    deadline = t0 + timedelta(minutes=c.handoff_after_minutes)
    before = timedelta(minutes=c.minutes_before_start)
    rep = ClosingReport()

    groups = start_groups(env.read_tonight(), env.clock(), after)
    log.info("Run %d of tonight's chain: %d start-time group(s) to cover; at most %d credits "
             "if every group qualifies", hop + 1, len(groups),
             len(groups) * cfg.odds_api.credits_per_pull)
    for g in groups:
        log.info("  %s: wake at %s", g.label(tz), iso_z(g.start_utc - before))

    last_handled: datetime | None = after
    for i, g in enumerate(groups):
        wake = g.start_utc - before
        if wake > deadline:
            remaining = groups[i:]
            if hop >= c.max_handoffs:
                rep.not_covered = [x.label(tz) for x in remaining]
                log.error("Hand-off limit (%d) reached; %d group(s) not covered: %s",
                          c.max_handoffs, len(remaining), "; ".join(rep.not_covered))
                break
            # Use this run's time first: wait until its limit, then hand off, so the
            # fresh run's own 5.5 hours start as late as possible.
            now = env.clock()
            if now < deadline:
                log.info("Next wake-up %s is past this run's %d-minute limit; waiting until %s "
                         "to hand off", iso_z(wake), c.handoff_after_minutes, iso_z(deadline))
                env.sleep((deadline - now).total_seconds())
            after_iso = iso_z(last_handled) if last_handled else ""
            log.info("Starting run %d for the remaining %d group(s) (next wake-up %s)",
                     hop + 2, len(remaining), iso_z(wake))
            env.dispatch(hop + 1, after_iso)
            rep.handed_off = True
            break

        now = env.clock()
        if now < wake:
            log.info("Sleeping %.0f minutes until %s for %s", (wake - now).total_seconds() / 60,
                     iso_z(wake), g.label(tz))
            env.sleep((wake - now).total_seconds())
            now = env.clock()
        last_handled = g.start_utc
        if now >= g.start_utc:
            rep.missed.append(g.label(tz))
            log.warning("Missed %s: it started before this run reached it", g.label(tz))
            continue

        tonight, picks = env.read_tonight(), env.read_picks()
        reason = qualifies(g, picks, tonight, c.qualifying_leans)
        if not reason:
            rep.skipped.append(g.label(tz))
            log.info("Skipped group %s: no picks and no %s; 0 credits", g.label(tz),
                     " / ".join(c.qualifying_leans))
            continue
        log.info("Pulling for group %s: %s", g.label(tz), reason)
        rep.credits += env.pull(now)
        rep.pulled.append(g.label(tz))

    log.info("SUMMARY closing run %d: %d group(s) pulled, %d skipped (no picks or leans), "
             "%d missed, %d not covered, %d credits%s", hop + 1, len(rep.pulled), len(rep.skipped),
             len(rep.missed), len(rep.not_covered), rep.credits,
             "; handed off to a fresh run" if rep.handed_off else "")
    return rep


# ---- Live wiring -------------------------------------------------------------
def make_pull(cfg: Config, client, sinks: Sequence[Sink],
              clock: Callable[[], datetime]) -> Callable[[datetime], int]:
    """Free /events call, budget check, then one paid pull of all games not yet started."""
    def pull(now: datetime) -> int:
        start, end = slate_window(now, cfg.slate_timezone, cfg.window_end_hour_local)
        events, usage = client.get_events(start, end)
        validate_events(events)
        if not events:
            log.info("No games left in the window; nothing pulled, 0 credits")
            return 0
        cost = cfg.odds_api.credits_per_pull
        check_budget(usage, cost, cfg.monthly_credits)
        log.info("Budget OK: %d used + %d for this pull <= %d monthly budget",
                 usage.used, cost, cfg.monthly_credits)
        odds, after = client.get_odds(start, end)
        snap = clock()
        spent = after.used - usage.used
        log.info("Credits for this pull: %d (x-requests-last=%s); %d remaining this month",
                 spent, after.last, after.remaining)
        cfg.raw_dir.mkdir(parents=True, exist_ok=True)
        (cfg.raw_dir / f"odds_{snap.strftime('%Y%m%dT%H%M%SZ')}.json").write_text(
            json.dumps(odds, indent=2))
        rows, dropped = build_rows(odds, snap, cfg.odds_api.bookmakers, cfg.odds_api.markets)
        log.info("%d rows built, %d dropped (game already started)", len(rows), dropped)
        if rows:
            for s in sinks:
                s.append(rows)
        return spent
    return pull


def github_dispatch(hop: int, after: str, session=None) -> None:
    """Start a fresh run of closing.yml (workflow_dispatch) with the GITHUB_TOKEN."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    if not repo or not token:
        raise HandoffError("GITHUB_REPOSITORY / GITHUB_TOKEN are not set; can't start a fresh run. "
                           "Press Run workflow by hand for the remaining groups.")
    s = session or requests.Session()
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    try:
        resp = s.post(url, json={"ref": ref, "inputs": {"hop": str(hop), "after": after}},
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"}, timeout=30)
    except requests.RequestException as exc:
        raise HandoffError(f"Starting the fresh run failed: {type(exc).__name__}. "
                           "Press Run workflow by hand for the remaining groups.") from None
    if resp.status_code not in (200, 204):
        raise HandoffError(f"Starting the fresh run failed: HTTP {resp.status_code} "
                           f"{resp.text[:200]}. Press Run workflow by hand for the remaining groups.")
    log.info("Started run %d of tonight's chain (workflow_dispatch on %s, after=%s)",
             hop + 1, ref, after or "-")


def run_live(cfg: Config, hop: int, after: datetime | None) -> ClosingReport:
    import time
    started = datetime.now(timezone.utc)
    from .morning import picks_sheet_tab
    client = OddsApiClient(cfg.odds_api, get_odds_api_key())
    session = authorized_session()
    sid = get_sheet_id()
    picks_tab = picks_sheet_tab(cfg, session, sid)          # creates Picks with its header if missing
    sinks: list[Sink] = [CsvSink(cfg.csv_path, COLUMNS),
                         SheetsSink(session, sid, cfg.sheet_tab, COLUMNS)]

    def clock() -> datetime:
        return datetime.now(timezone.utc)

    env = Env(clock=clock, sleep=time.sleep,
              read_tonight=lambda: read_tab(session, sid, cfg.morning.tonight_tab),
              read_picks=lambda: [r for _, r in picks_tab.read_rows()],
              pull=make_pull(cfg, client, sinks, clock),
              dispatch=lambda h, a: github_dispatch(h, a))
    return run(cfg, env, hop, after, started)


# ---- Dry run -----------------------------------------------------------------
class SimClock:
    """A clock that jumps forward when slept on."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def run_dry(cfg: Config, hop: int, after: datetime | None) -> ClosingReport:
    """Tonight priced from the newest real fixtures, simulated evening, no API calls."""
    from .games import et_date
    from .morning import dry_run_snapshots, fixture_fetcher, memory_tabs
    from .morning import run as morning_run
    from . import tonight_lines

    snapshots = dry_run_snapshots(cfg)
    if not snapshots:
        raise ConfigError("No real odds fixture in tests/fixtures/real for --dry-run")
    snap = parse_time(snapshots[0]["snapshot_time_utc"])
    day = et_date(min(parse_time(r["start_time_utc"]) for r in snapshots))
    tabs = memory_tabs(cfg)
    morning_run(cfg, day, fixture_fetcher(cfg.morning.fixtures_dir), tabs, snapshots, snap)
    tonight_lines.apply(tabs.tonight, snapshots, cfg.pricing, snap, cfg.morning.match_tolerance_hours)
    real = sorted((cfg.odds_fixture.parent / "real").glob("odds_*.json"))[-1]
    odds = json.loads(real.read_text())

    clock = SimClock(snap)

    def pull(now: datetime) -> int:
        rows, dropped = build_rows(odds, now, cfg.odds_api.bookmakers, cfg.odds_api.markets)
        log.info("DRY RUN: a live pull here costs %d credits; %d rows (%d dropped as started)",
                 cfg.odds_api.credits_per_pull, len(rows), dropped)
        log.info("DRY RUN: would append %d rows to Odds_Snapshots", len(rows))
        return cfg.odds_api.credits_per_pull

    def dispatch(h: int, a: str) -> None:
        log.info("DRY RUN: would start run %d of the chain (hop=%d, after=%s)", h + 1, h, a or "-")

    log.info("DRY RUN: simulated clock starts at %s (fixture snapshot); no Picks rows, so only "
             "groups with auto leans qualify", iso_z(snap))
    env = Env(clock=clock, sleep=clock.sleep,
              read_tonight=lambda: [r for _, r in tabs.tonight.read_rows()],
              read_picks=lambda: [], pull=pull, dispatch=dispatch)
    return run(cfg, env, hop, after, snap)


# ---- CLI ---------------------------------------------------------------------
def _annotate(level: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        clean = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        sys.stdout.write(f"::{level} title=closing::{clean}\n")
        sys.stdout.flush()


class _AnnotationHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            _annotate("error", msg)
        elif record.levelno >= logging.WARNING:
            _annotate("warning", msg)
        elif msg.startswith(("SUMMARY", "Skipped group", "Pulling for group", "Credits for this pull",
                             "Started run", "DRY RUN: would start")):
            _annotate("notice", msg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="saved fixtures and a simulated clock; no API calls, nothing written")
    parser.add_argument("--hop", type=int, default=0,
                        help="fresh runs already started tonight (set by the hand-off)")
    parser.add_argument("--after", default="",
                        help="only groups starting after this UTC time (set by the hand-off)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pkg_log = logging.getLogger("nhl_betting")
    if not any(isinstance(h, _AnnotationHandler) for h in pkg_log.handlers):
        pkg_log.addHandler(_AnnotationHandler())
    try:
        cfg = load_config(args.config) if args.config else load_config()
        after = parse_time(args.after) if args.after else None
        if args.dry_run:
            run_dry(cfg, args.hop, after)
        else:
            run_live(cfg, args.hop, after)
        return 0
    except BudgetError as exc:
        log.error("STOPPED BEFORE PULL (remaining groups not pulled): %s", exc)
        return 2
    except SchemaError as exc:
        log.error("API RESPONSE CHANGED SHAPE: %s", exc)
        return 3
    except (ConfigError, OddsApiError, SheetsError, HandoffError, OSError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:
        log.exception("Unexpected %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
