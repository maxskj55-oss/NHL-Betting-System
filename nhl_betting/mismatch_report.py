"""How often FanDuel and Pinnacle hang different puck lines and totals.

Reads Odds_Snapshots (read-only, no Odds API credits) and compares the two books
game by game, using each game's latest pre-start snapshot so repeated pulls
aren't double-counted.

    python -m nhl_betting.mismatch_report            # read the Google Sheet
    python -m nhl_betting.mismatch_report --csv FILE # read a downloaded CSV
    python -m nhl_betting.mismatch_report --dry-run  # saved real fixture, no API calls
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config
from .sheets import SheetsError, authorized_session, get_sheet_id, read_tab
from .snapshots import build_rows, parse_time

log = logging.getLogger("nhl_betting.mismatch_report")
FIXTURE = Path(__file__).resolve().parent.parent / "tests/fixtures/real/odds_20261017T153438Z.json"
ET = ZoneInfo("America/New_York")


@dataclass
class GameCompare:
    event_id: str
    game: str
    night: str                      # ET date of puck drop
    totals: tuple | None = None     # (fanduel, pinnacle) total, if both posted
    spreads: tuple | None = None    # (fanduel, pinnacle) home-team puck line, if both posted


@dataclass
class Summary:
    games: int = 0
    totals_compared: int = 0
    totals_differ: int = 0
    totals_gaps: Counter = field(default_factory=Counter)
    spreads_compared: int = 0
    spreads_differ: int = 0
    favorite_flips: int = 0
    any_differ: int = 0


def _num(v) -> float | None:
    if v in ("", None):
        return None
    return float(v)


def latest_per_game(rows: list[dict]) -> list[dict]:
    """Keep only rows from each game's most recent snapshot."""
    latest: dict[str, str] = {}
    for r in rows:
        eid, snap = r["odds_event_id"], str(r["snapshot_time_utc"])
        if snap > latest.get(eid, ""):
            latest[eid] = snap
    return [r for r in rows if str(r["snapshot_time_utc"]) == latest[r["odds_event_id"]]]


def compare_games(rows: list[dict]) -> list[GameCompare]:
    by_game: dict[str, dict] = defaultdict(dict)
    meta: dict[str, GameCompare] = {}
    for r in latest_per_game(rows):
        eid = r["odds_event_id"]
        if eid not in meta:
            start = parse_time(str(r["start_time_utc"])).astimezone(ET)
            meta[eid] = GameCompare(eid, f"{r['away_team']} @ {r['home_team']}",
                                    start.date().isoformat())
        if r["market"] == "totals" and r["side"] == "Over":
            by_game[eid][("totals", r["book"])] = _num(r["line"])
        if r["market"] == "spreads" and r["side"] == r["home_team"]:
            by_game[eid][("spreads", r["book"])] = _num(r["line"])
    for eid, g in meta.items():
        lines = by_game[eid]
        for market in ("totals", "spreads"):
            fd, pin = lines.get((market, "fanduel")), lines.get((market, "pinnacle"))
            if fd is not None and pin is not None:
                setattr(g, market, (fd, pin))
    return list(meta.values())


def summarize(games: list[GameCompare]) -> Summary:
    s = Summary(games=len(games))
    for g in games:
        differs = False
        if g.totals:
            s.totals_compared += 1
            if g.totals[0] != g.totals[1]:
                s.totals_differ += 1
                s.totals_gaps[abs(g.totals[0] - g.totals[1])] += 1
                differs = True
        if g.spreads:
            s.spreads_compared += 1
            if g.spreads[0] != g.spreads[1]:
                s.spreads_differ += 1
                differs = True
                if (g.spreads[0] < 0) != (g.spreads[1] < 0):
                    s.favorite_flips += 1
        s.any_differ += differs
    return s


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"


def render(games: list[GameCompare]) -> str:
    """Markdown report: overall, per night, and the mismatched games."""
    overall = summarize(games)
    gaps = ", ".join(f"{k:g} goal: {v}" for k, v in sorted(overall.totals_gaps.items())) or "none"
    lines = [
        "## Line mismatch report (FanDuel vs Pinnacle)",
        "",
        "| Measure | Result |",
        "| --- | --- |",
        f"| Games (latest snapshot each) | {overall.games} |",
        f"| Games with any mismatch | {_pct(overall.any_differ, overall.games)} |",
        f"| Totals differ | {_pct(overall.totals_differ, overall.totals_compared)} |",
        f"| Totals gap size | {gaps} |",
        f"| Puck lines differ | {_pct(overall.spreads_differ, overall.spreads_compared)} |",
        f"| Puck-line favorite flipped | {overall.favorite_flips} |",
        "",
        "### By night (ET)",
        "",
        "| Night | Games | Totals differ | Puck lines differ |",
        "| --- | --- | --- | --- |",
    ]
    nights: dict[str, list[GameCompare]] = defaultdict(list)
    for g in games:
        nights[g.night].append(g)
    for night in sorted(nights):
        s = summarize(nights[night])
        lines.append(f"| {night} | {s.games} | {_pct(s.totals_differ, s.totals_compared)} | "
                     f"{_pct(s.spreads_differ, s.spreads_compared)} |")
    diff_games = [g for g in games if (g.totals and g.totals[0] != g.totals[1])
                  or (g.spreads and g.spreads[0] != g.spreads[1])]
    if diff_games:
        lines += ["", "### Mismatched games", "",
                  "| Night | Game | Totals FD / PIN | Home puck line FD / PIN |",
                  "| --- | --- | --- | --- |"]
        for g in sorted(diff_games, key=lambda g: (g.night, g.game)):
            t = f"{g.totals[0]:g} / {g.totals[1]:g}" if g.totals else "-"
            sp = f"{g.spreads[0]:+g} / {g.spreads[1]:+g}" if g.spreads else "-"
            lines.append(f"| {g.night} | {g.game} | {t} | {sp} |")
    return "\n".join(lines) + "\n"


def headline(games: list[GameCompare]) -> str:
    s = summarize(games)
    return (f"{s.games} games: totals differ {_pct(s.totals_differ, s.totals_compared)}, "
            f"puck lines differ {_pct(s.spreads_differ, s.spreads_compared)}, "
            f"favorite flips {s.favorite_flips}")


def _rows_from_fixture() -> list[dict]:
    odds = json.loads(FIXTURE.read_text())
    cfg = load_config().odds_api
    first = min(parse_time(e["commence_time"]) for e in odds)
    rows, _ = build_rows(odds, first - timedelta(hours=1), cfg.bookmakers, cfg.markets)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--dry-run", action="store_true", help="saved real fixture, no API calls")
    src.add_argument("--csv", help="read rows from a downloaded Odds_Snapshots CSV")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.dry_run:
            rows = _rows_from_fixture()
            log.info("DRY RUN: using %s", FIXTURE.name)
        elif args.csv:
            with open(args.csv, newline="") as f:
                rows = list(csv.DictReader(f))
        else:
            cfg = load_config()
            rows = read_tab(authorized_session(), get_sheet_id(), cfg.sheet_tab)
        log.info("Read %d Odds_Snapshots rows", len(rows))
        games = compare_games(rows)
        report = render(games)
        log.info("\n%s", report)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write(report)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            sys.stdout.write(f"::notice title=mismatch_report::{headline(games)}\n")
        return 0
    except (ConfigError, SheetsError, OSError, KeyError, ValueError) as exc:
        log.error("%s", exc)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            sys.stdout.write(f"::error title=mismatch_report::{exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
