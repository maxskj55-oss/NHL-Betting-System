"""Pull tonight's FanDuel and Pinnacle game lines and append them to Odds_Snapshots.

    python -m nhl_betting.pull_odds            # live: 0-credit check, then a 3-credit pull
    python -m nhl_betting.pull_odds --dry-run  # saved fixtures, no API calls at all

Exit codes: 0 ok (including "no games tonight"), 1 error, 2 over budget, 3 schema change.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config, ConfigError, get_odds_api_key, load_config
from .odds_api import BudgetError, OddsApiClient, OddsApiError, check_budget, iso_z
from .sheets import SheetsError, SheetsSink, authorized_session, get_sheet_id
from .sinks import CsvSink, PrintSink, Sink
from .snapshots import (COLUMNS, SchemaError, build_rows, coverage_gaps, line_mismatches,
                        parse_time, validate_events)
from .teams import team_code

log = logging.getLogger("nhl_betting.pull_odds")


def slate_window(now: datetime, tz_name: str, end_hour: int) -> tuple[datetime, datetime]:
    """From now until `end_hour` local time on the next morning (UTC datetimes)."""
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    end_date = local.date() + timedelta(days=0 if local.hour < end_hour else 1)
    end = datetime.combine(end_date, time(end_hour), tzinfo=tz)
    return now.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _load_json(path: Path):
    return json.loads(Path(path).read_text())


def run_dry(cfg: Config, as_of: datetime | None, sink: Sink | None = None) -> int:
    events = _load_json(cfg.events_fixture)
    odds = _load_json(cfg.odds_fixture)
    validate_events(events)
    if as_of is None:
        # Fixture games are in the past, so pretend it's 3 hours before the first one.
        first = min(parse_time(e["commence_time"]) for e in odds)
        as_of = first - timedelta(hours=3)
    log.info("DRY RUN: no API calls. Fixtures: %s, %s", cfg.events_fixture.name, cfg.odds_fixture.name)
    log.info("DRY RUN: snapshot time %s; %d events in fixture", iso_z(as_of), len(events))
    log.info("DRY RUN: a live run would check credits (free), then spend %d credits",
             cfg.odds_api.credits_per_pull)
    rows, dropped = build_rows(odds, as_of, cfg.odds_api.bookmakers, cfg.odds_api.markets)
    log.info("%d rows built, %d dropped (game already started)", len(rows), dropped)
    report_gaps(events, odds, cfg)
    check_team_names(events)
    (sink or PrintSink(COLUMNS)).append(rows)
    return 0


def report_gaps(events: list[dict], odds: list[dict], cfg: Config) -> None:
    """Warn (shown on the run page) when a game, book or market is missing."""
    diffs = line_mismatches(odds, cfg.odds_api.bookmakers)
    if diffs:
        log.warning("Books hang different lines (not directly comparable): %s", "; ".join(diffs))
    gaps = coverage_gaps(events, odds, cfg.odds_api.bookmakers, cfg.odds_api.markets)
    if not gaps:
        log.info("Coverage complete: every game has every book and market")
        return
    log.warning("Incomplete lines (%d gaps; pulling later may fill them): %s",
                len(gaps), "; ".join(gaps))


def check_team_names(events: list[dict]) -> None:
    """Warn about team names the NHL lookup table doesn't know (needed for step 2 matching)."""
    unknown = sorted({ev[side] for ev in events for side in ("home_team", "away_team")
                      if team_code(ev[side]) is None})
    if unknown:
        log.warning("Unknown team name(s), add to nhl_betting/teams.py: %s", ", ".join(unknown))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_sinks(cfg: Config) -> list[Sink]:
    """CSV backup always; the Google Sheet too when sink: sheets.

    Built before any paid call so a missing Google secret costs no credits.
    """
    sinks: list[Sink] = [CsvSink(cfg.csv_path, COLUMNS)]
    if cfg.sink == "sheets":
        sinks.append(SheetsSink(authorized_session(), get_sheet_id(), cfg.sheet_tab, COLUMNS))
    elif cfg.sink != "csv":
        raise ConfigError(f"Unknown output.sink {cfg.sink!r} (use 'sheets' or 'csv').")
    return sinks


def run_live(cfg: Config, client: OddsApiClient | None = None,
             sinks: list[Sink] | None = None,
             clock: Callable[[], datetime] = _utc_now) -> int:
    client = client or OddsApiClient(cfg.odds_api, get_odds_api_key())
    sinks = sinks if sinks is not None else build_sinks(cfg)
    now = clock()
    start, end = slate_window(now, cfg.slate_timezone, cfg.window_end_hour_local)
    log.info("Slate window %s to %s", iso_z(start), iso_z(end))

    # 1. Free call: tonight's games + current credit usage.
    events, usage = client.get_events(start, end)
    validate_events(events)
    if not events:
        log.info("No games tonight; nothing pulled, 0 credits spent.")
        return 0
    log.info("%d games tonight", len(events))

    # 2. Credit check before the paid call.
    cost = cfg.odds_api.credits_per_pull
    check_budget(usage, cost, cfg.monthly_credits)
    log.info("Budget OK: %d used + %d for this pull <= %d monthly budget",
             usage.used, cost, cfg.monthly_credits)

    # 3. Paid call.
    odds, after = client.get_odds(start, end)
    snapshot_time = clock()
    spent = after.used - usage.used
    log.info("Credits for this run: %d (x-requests-last=%s); %d remaining this month",
             spent, after.last, after.remaining)
    if after.last is not None and after.last != cost:
        log.warning("Expected this pull to cost %d credits, header says %d", cost, after.last)

    # Keep the raw response: it becomes a real-data test fixture.
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = cfg.raw_dir / f"odds_{snapshot_time.strftime('%Y%m%dT%H%M%SZ')}.json"
    raw_path.write_text(json.dumps(odds, indent=2))
    log.info("Saved raw response to %s", raw_path)

    if not odds:
        log.warning("Odds API returned no priced games (lines may not be posted yet).")
        return 0

    rows, dropped = build_rows(odds, snapshot_time, cfg.odds_api.bookmakers, cfg.odds_api.markets)
    log.info("%d rows built, %d dropped (game already started)", len(rows), dropped)
    report_gaps(events, odds, cfg)
    check_team_names(events)
    for s in sinks:
        s.append(rows)
    return 0


def _annotate(level: str, message: str) -> None:
    """In GitHub Actions, show a message on the run's summary page (level: error/warning/notice)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        clean = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        sys.stdout.write(f"::{level} title=pull_odds::{clean}\n")
        sys.stdout.flush()


class _AnnotationHandler(logging.Handler):
    """Mirror WARNING+ log lines and credit lines to GitHub annotations."""

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            _annotate("error", msg)
        elif record.levelno >= logging.WARNING:
            _annotate("warning", msg)
        elif msg.startswith(("Credits for this run", "Appended", "No games tonight", "DRY RUN: would")):
            _annotate("notice", msg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="use saved fixtures; no API calls, nothing written")
    parser.add_argument("--as-of", help="dry run only: snapshot time, e.g. 2026-10-17T20:00:00Z")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pkg_log = logging.getLogger("nhl_betting")
    if not any(isinstance(h, _AnnotationHandler) for h in pkg_log.handlers):
        pkg_log.addHandler(_AnnotationHandler())
    try:
        cfg = load_config(args.config) if args.config else load_config()
        if args.dry_run:
            return run_dry(cfg, parse_time(args.as_of) if args.as_of else None)
        return run_live(cfg)
    except BudgetError as exc:
        log.error("STOPPED BEFORE PULL: %s", exc)
        return 2
    except SchemaError as exc:
        log.error("API RESPONSE CHANGED SHAPE: %s", exc)
        return 3
    except (ConfigError, OddsApiError, SheetsError, OSError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:  # unexpected: still show it on the run page
        log.exception("Unexpected %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
