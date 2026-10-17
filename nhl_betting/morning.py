"""Daily NHL job: Game_Map, Results and Tonight from the NHL web API.

    python -m nhl_betting.morning                       # live: NHL API + Google Sheet
    python -m nhl_betting.morning --dry-run             # saved fixtures, nothing written
    python -m nhl_betting.morning --dry-run --date 2026-10-17

Uses no Odds API credits: it never calls the Odds API (it reads Odds_Snapshots
from the sheet instead). Order of work:

1. Game_Map: map Odds API events in Odds_Snapshots to NHL game_ids (append only).
2. Results: settle final games from the last few days not yet in Results.
3. Tonight: move past days' rows to Tonight_Archive, then add or refresh one row
   per game today. Only code-owned game cells are written; claude_prob,
   claude_reason and the odds columns (Pull odds, step 3) are never touched.

Exit codes: 0 ok, 1 error, 3 API shape changed or a score can't be settled.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from .config import Config, ConfigError, load_config
from .games import Game, et_date, parse_games, teams_that_played
from .matching import events_from_snapshots, match_events
from .nhl_api import NhlApiClient, NhlApiError, SchemaError
from .odds_api import iso_z
from .pricing import SettlementError, settle_spread, settlement_score, total_outcome
from .sheets import SheetsError, SheetTab, authorized_session, get_sheet_id, read_tab
from .sinks import CsvSink
from .snapshots import SchemaError as OddsSchemaError
from .snapshots import build_rows, parse_time
from .tabs import MemoryTab, Tab, norm

log = logging.getLogger("nhl_betting.morning")

# ---- Sheet contract (shared with the claude.ai Project's rulebook) -----------
# Tonight: this job writes one placeholder row per game (market blank); Pull odds
# turns it into one row per game x market x side (tonight_lines.py).
# claude_line was added at the end in step 3 so existing columns keep their order.
TONIGHT_COLUMNS: tuple[str, ...] = (
    "game_id", "game_date_et", "start_time_utc", "start_time_et", "home", "away",
    "home_b2b", "away_b2b", "projected_goalies", "market", "side", "line",
    "fanduel_price", "baseline_prob", "fair_price", "baseline_ev", "auto_lean",
    "claude_prob", "claude_reason", "adjusted_ev", "stake_units", "claude_line",
)
TONIGHT_OWNED: tuple[str, ...] = (  # what this job writes
    "game_id", "game_date_et", "start_time_utc", "start_time_et", "home", "away",
    "home_b2b", "away_b2b",
)
TONIGHT_PROTECTED: tuple[str, ...] = ("claude_prob", "claude_reason")   # Claude's; never written
TONIGHT_FORMULAS: tuple[str, ...] = ("adjusted_ev", "stake_units")     # formulas only, never values
TONIGHT_KEY: tuple[str, ...] = ("game_id", "market", "side")

# Same columns plus archived_at_utc; claude_line comes after it because it was
# added later and existing columns keep their order.
ARCHIVE_COLUMNS: tuple[str, ...] = TONIGHT_COLUMNS[:-1] + ("archived_at_utc", "claude_line")
ARCHIVE_KEY: tuple[str, ...] = ("game_date_et",) + TONIGHT_KEY

RESULTS_COLUMNS: tuple[str, ...] = (
    "game_id", "game_date_et", "start_time_utc", "home", "away",
    "home_score", "away_score", "ot_so", "ml_winner", "pl_home_-1.5", "pl_away_-1.5",
    "total_goals", "total_line_fanduel", "total_result_fanduel",
    "total_line_pinnacle", "total_result_pinnacle", "settled_at_utc",
    "closing_fanduel", "closing_pinnacle", "clv",
)
RESULTS_PROTECTED: tuple[str, ...] = ("closing_fanduel", "closing_pinnacle", "clv")  # step 4

GAME_MAP_COLUMNS: tuple[str, ...] = (
    "odds_event_id", "game_id", "game_date_et", "home", "away",
    "odds_start_utc", "nhl_start_utc", "mapped_at_utc",
)

TOTALS_BOOKS: tuple[str, ...] = ("fanduel", "pinnacle")


@dataclass
class MorningTabs:
    tonight: Tab
    archive: Tab
    results: Tab
    game_map: Tab


@dataclass
class Report:
    mapped: int = 0
    settled: int = 0
    tonight_added: int = 0
    tonight_updated: int = 0
    archived: int = 0
    problems: list[str] = field(default_factory=list)   # settlement/shape problems -> exit 3


# ---- Pure builders -----------------------------------------------------------
def _local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def tonight_row(g: Game, played_yesterday: set[str], tz: ZoneInfo) -> dict:
    return {
        "game_id": g.game_id,
        "game_date_et": g.game_date.isoformat(),
        "start_time_utc": iso_z(g.start_utc),
        "start_time_et": _local(g.start_utc, tz),
        "home": g.home,
        "away": g.away,
        "home_b2b": g.home in played_yesterday,
        "away_b2b": g.away in played_yesterday,
    }


def last_totals_line(snapshots: Sequence[dict], event_ids: set[str], book: str) -> float | None:
    """Totals line from the latest snapshot of this game at this book (pre-start rows only)."""
    best: tuple[str, float] | None = None
    for r in snapshots:
        if (norm(r.get("odds_event_id")) in event_ids and r.get("book") == book
                and r.get("market") == "totals" and str(r.get("side", "")).lower() == "over"
                and norm(r.get("line")) != ""):
            snap = norm(r.get("snapshot_time_utc"))
            if best is None or snap > best[0]:
                best = (snap, float(r["line"]))
    return best[1] if best else None


def result_row(g: Game, snapshots: Sequence[dict], event_ids: set[str], now: datetime) -> dict:
    s = settlement_score(g.home_score, g.away_score, g.last_period)
    row = {
        "game_id": g.game_id,
        "game_date_et": g.game_date.isoformat(),
        "start_time_utc": iso_z(g.start_utc),
        "home": g.home,
        "away": g.away,
        "home_score": s.home,
        "away_score": s.away,
        "ot_so": s.ot_so,
        "ml_winner": g.home if s.margin_home > 0 else g.away,
        "pl_home_-1.5": settle_spread(s.margin_home, -1.5),
        "pl_away_-1.5": settle_spread(-s.margin_home, -1.5),
        "total_goals": s.total,
        "settled_at_utc": iso_z(now),
    }
    for book in TOTALS_BOOKS:
        line = last_totals_line(snapshots, event_ids, book)
        row[f"total_line_{book}"] = "" if line is None else line
        row[f"total_result_{book}"] = "" if line is None else total_outcome(s.total, line)
    return row


# ---- Steps -------------------------------------------------------------------
def update_game_map(tabs: MorningTabs, snapshots: Sequence[dict], games: list[Game],
                    dates: set[date], tol_hours: float, now: datetime, rep: Report) -> dict[int, set[str]]:
    """Append new mappings; return {game_id: {odds_event_ids}} for all known mappings."""
    existing = tabs.game_map.read_rows()
    known = {norm(r["odds_event_id"]) for _, r in existing}
    events = [e for e in events_from_snapshots(snapshots) if e.odds_event_id not in known]
    in_window = [e for e in events if et_date(e.start_utc) in dates]
    if len(in_window) < len(events):
        log.warning("Game_Map: %d unmapped Odds API event(s) start outside the lookback window; "
                    "not mapped", len(events) - len(in_window))
    matched, problems = match_events(in_window, games, tol_hours)
    for p in problems:
        log.warning("Game_Map: %s", p)
    new_rows = []
    for e in in_window:
        g = matched.get(e.odds_event_id)
        if g is None:
            continue
        new_rows.append({
            "odds_event_id": e.odds_event_id, "game_id": g.game_id,
            "game_date_et": g.game_date.isoformat(), "home": g.home, "away": g.away,
            "odds_start_utc": iso_z(e.start_utc), "nhl_start_utc": iso_z(g.start_utc),
            "mapped_at_utc": iso_z(now),
        })
    rep.mapped = tabs.game_map.append_rows(new_rows, ("odds_event_id",))
    by_game: dict[int, set[str]] = {}
    for r in [r for _, r in existing] + new_rows:
        by_game.setdefault(int(float(r["game_id"])), set()).add(norm(r["odds_event_id"]))
    return by_game


def update_results(tabs: MorningTabs, games: list[Game], today: date, snapshots: Sequence[dict],
                   event_map: dict[int, set[str]], now: datetime, rep: Report) -> None:
    have = {norm(r["game_id"]) for _, r in tabs.results.read_rows()}
    rows = []
    for g in sorted(games, key=lambda g: (g.start_utc, g.game_id)):
        if g.game_date >= today or str(g.game_id) in have:
            continue
        if not g.is_final:
            if g.schedule_state == "OK":
                log.warning("Results: %s on %s is %s, not final yet; will settle on a later run",
                            g.matchup, g.game_date, g.state)
            else:
                log.info("Results: %s on %s is %s; skipped", g.matchup, g.game_date, g.schedule_state)
            continue
        try:
            rows.append(result_row(g, snapshots, event_map.get(g.game_id, set()), now))
        except SettlementError as exc:
            msg = f"Results: {g.matchup} ({g.game_id}) not settled: {exc}"
            log.error("%s", msg)
            rep.problems.append(msg)
    rep.settled = tabs.results.append_rows(rows, ("game_id",))


def update_tonight(tabs: MorningTabs, today_games: list[Game], played_yesterday: set[str],
                   today: date, tz: ZoneInfo, now: datetime, rep: Report) -> None:
    today_s = today.isoformat()
    current = tabs.tonight.read_rows()

    # 1. Past days' rows -> Tonight_Archive (skipping any already there), then remove them.
    old = [(n, r) for n, r in current if norm(r.get("game_date_et")) != today_s]
    if old:
        archived_keys = {tuple(norm(r.get(c)) for c in ARCHIVE_KEY) for _, r in tabs.archive.read_rows()}
        to_archive = [dict(r, archived_at_utc=iso_z(now)) for _, r in old
                      if tuple(norm(r.get(c)) for c in ARCHIVE_KEY) not in archived_keys]
        tabs.archive.append_rows(to_archive, ARCHIVE_KEY)
        tabs.tonight.delete_rows([n for n, _ in old])
        rep.archived = len(old)
        current = tabs.tonight.read_rows()

    # 2. Today's games: refresh owned cells where they changed, add missing games.
    by_game: dict[str, list[tuple[int, dict]]] = {}
    for n, r in current:
        by_game.setdefault(norm(r.get("game_id")), []).append((n, r))
    playing = [g for g in today_games if g.is_scheduled_ok]
    updates, new_rows = [], []
    for g in sorted(playing, key=lambda g: (g.start_utc, g.game_id)):
        want = tonight_row(g, played_yesterday, tz)
        rows = by_game.pop(str(g.game_id), [])
        if not rows:
            new_rows.append(want)
            continue
        for n, r in rows:
            changed = {c: v for c, v in want.items() if norm(r.get(c)) != norm(v)}
            if changed:
                updates.append((n, changed))
    for gid, rows in by_game.items():
        log.warning("Tonight: game %s is in the tab but not on today's NHL schedule "
                    "(postponed?); left as is", gid)
    for g in today_games:
        if not g.is_scheduled_ok:
            log.warning("Tonight: %s is %s; not listed", g.matchup, g.schedule_state)
    rep.tonight_updated = tabs.tonight.update_cells(updates)
    rep.tonight_added = tabs.tonight.append_rows(new_rows, TONIGHT_KEY)


def run(cfg: Config, today: date, fetch: Callable[[date], list[Game]], tabs: MorningTabs,
        snapshots: Sequence[dict], now: datetime) -> Report:
    m = cfg.morning
    if m.results_lookback_days < 1:
        raise ConfigError("morning.results_lookback_days must be at least 1")
    tz = ZoneInfo(cfg.slate_timezone)
    dates = [today - timedelta(days=k) for k in range(m.results_lookback_days, -1, -1)]
    by_date = {d: fetch(d) for d in dates}
    games = [g for d in dates for g in by_date[d]]
    rep = Report()

    event_map = update_game_map(tabs, snapshots, games, set(dates), m.match_tolerance_hours, now, rep)
    update_results(tabs, games, today, snapshots, event_map, now, rep)
    played_yesterday = teams_that_played(by_date[today - timedelta(days=1)])
    update_tonight(tabs, by_date[today], played_yesterday, today, tz, now, rep)

    log.info("SUMMARY %s: Tonight %d games (%d added, %d updated, %d old rows archived); "
             "Results %d settled; Game_Map %d mapped; 0 Odds API credits",
             today, len([g for g in by_date[today] if g.is_scheduled_ok]), rep.tonight_added,
             rep.tonight_updated, rep.archived, rep.settled, rep.mapped)
    return rep


# ---- Live and dry-run wiring -------------------------------------------------
def fixture_fetcher(fixtures_dir: Path) -> Callable[[date], list[Game]]:
    def fetch(d: date) -> list[Game]:
        path = Path(fixtures_dir) / f"score_{d.isoformat()}.json"
        if not path.exists():
            raise ConfigError(f"No fixture {path.name} in {fixtures_dir} for --dry-run on this date")
        return parse_games(json.loads(path.read_text()), d.isoformat())
    return fetch


def live_fetcher(cfg: Config) -> Callable[[date], list[Game]]:
    client = NhlApiClient(cfg.nhl_api_base_url, cfg.nhl_api_timeout_seconds)
    raw_dir = cfg.morning.raw_dir

    def fetch(d: date) -> list[Game]:
        payload = client.get_score(d)
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"score_{d.isoformat()}.json").write_text(json.dumps(payload, indent=2))
        return parse_games(payload, d.isoformat())
    return fetch


def memory_tabs(cfg: Config) -> MorningTabs:
    m = cfg.morning
    return MorningTabs(
        tonight=MemoryTab(m.tonight_tab, TONIGHT_COLUMNS, TONIGHT_PROTECTED,
                          formulas=TONIGHT_FORMULAS),
        archive=MemoryTab(m.archive_tab, ARCHIVE_COLUMNS),
        results=MemoryTab(m.results_tab, RESULTS_COLUMNS, RESULTS_PROTECTED),
        game_map=MemoryTab(m.game_map_tab, GAME_MAP_COLUMNS),
    )


def sheet_tabs(cfg: Config, session, sid: str) -> MorningTabs:
    m = cfg.morning
    return MorningTabs(
        tonight=tonight_sheet_tab(cfg, session, sid),
        archive=SheetTab(session, sid, m.archive_tab, ARCHIVE_COLUMNS).ensure(),
        results=SheetTab(session, sid, m.results_tab, RESULTS_COLUMNS, RESULTS_PROTECTED).ensure(),
        game_map=SheetTab(session, sid, m.game_map_tab, GAME_MAP_COLUMNS).ensure(),
    )


def tonight_sheet_tab(cfg: Config, session, sid: str) -> SheetTab:
    return SheetTab(session, sid, cfg.morning.tonight_tab, TONIGHT_COLUMNS, TONIGHT_PROTECTED,
                    formulas=TONIGHT_FORMULAS).ensure()


def dry_run_snapshots(cfg: Config) -> list[dict]:
    """Odds_Snapshots rows built from the newest real /odds fixture."""
    real = sorted((cfg.odds_fixture.parent / "real").glob("odds_*.json"))
    if not real:
        return []
    stamp = real[-1].stem.split("_", 1)[1]
    snap = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    rows, _ = build_rows(json.loads(real[-1].read_text()), snap,
                         cfg.odds_api.bookmakers, cfg.odds_api.markets)
    log.info("DRY RUN: %d Odds_Snapshots rows from %s", len(rows), real[-1].name)
    return rows


def print_table(title: str, columns: Sequence[str], rows: Sequence[dict]) -> None:
    log.info("DRY RUN: would write %d rows to %s", len(rows), title)
    if not rows:
        return
    cols = [c for c in columns if any(norm(r.get(c)) for r in rows)]
    widths = {c: max([len(c)] + [len(norm(r.get(c))) for r in rows]) for c in cols}
    lines = ["  ".join(c.ljust(widths[c]) for c in cols)]
    lines.append("-" * len(lines[0]))
    lines += ["  ".join(norm(r.get(c)).ljust(widths[c]) for c in cols) for r in rows]
    log.info("%s:\n%s", title, "\n".join(lines))


def save_csv_copies(cfg: Config, tabs: MorningTabs) -> None:
    """CSV copy of every row this run appended (uploaded with the run)."""
    for tab in (tabs.game_map, tabs.results, tabs.tonight, tabs.archive):
        rows = getattr(tab, "appended", None)
        if rows:
            CsvSink(cfg.morning.csv_dir / f"{tab.name}.csv", tab.columns).append(rows)


class _RecordingTab:
    """Wrap a SheetTab and remember appended rows for the CSV copy."""

    def __init__(self, inner: SheetTab):
        self._inner = inner
        self.appended: list[dict] = []

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def append_rows(self, rows, key_cols):
        n = self._inner.append_rows(rows, key_cols)
        self.appended.extend(rows)
        return n


def _et_today(cfg: Config, now: datetime) -> date:
    return now.astimezone(ZoneInfo(cfg.slate_timezone)).date()


def run_dry(cfg: Config, today: date | None) -> Report:
    tabs = memory_tabs(cfg)
    snapshots = dry_run_snapshots(cfg)
    if today is None:
        today = max(et_date(parse_time(r["start_time_utc"])) for r in snapshots) \
            if snapshots else date(2026, 10, 17)
    log.info("DRY RUN: no Sheets writes, NHL data from %s, date %s", cfg.morning.fixtures_dir, today)
    now = datetime.combine(today, datetime.min.time(), ZoneInfo(cfg.slate_timezone)).replace(hour=8)
    rep = run(cfg, today, fixture_fetcher(cfg.morning.fixtures_dir), tabs, snapshots,
              now.astimezone(timezone.utc))
    for tab in (tabs.game_map, tabs.results, tabs.tonight):
        print_table(tab.name, tab.columns, tab.appended)
    return rep


def run_live(cfg: Config, today: date | None) -> Report:
    session = authorized_session()
    sid = get_sheet_id()
    snapshots = read_tab(session, sid, cfg.sheet_tab)
    tabs = sheet_tabs(cfg, session, sid)
    tabs = MorningTabs(*(_RecordingTab(t) for t in (tabs.tonight, tabs.archive,
                                                     tabs.results, tabs.game_map)))
    now = datetime.now(timezone.utc)
    rep = run(cfg, today or _et_today(cfg, now), live_fetcher(cfg), tabs, snapshots, now)
    save_csv_copies(cfg, tabs)
    return rep


# ---- CLI ---------------------------------------------------------------------
def _annotate(level: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        clean = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        sys.stdout.write(f"::{level} title=morning::{clean}\n")
        sys.stdout.flush()


class _AnnotationHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            _annotate("error", msg)
        elif record.levelno >= logging.WARNING:
            _annotate("warning", msg)
        elif msg.startswith(("SUMMARY", "DRY RUN: would", "Appended", "Updated", "Deleted")):
            _annotate("notice", msg.split("\n", 1)[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="use saved fixtures; no API calls, nothing written")
    parser.add_argument("--date", type=date.fromisoformat,
                        help="ET date to treat as today (default: today; dry run: fixture date)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pkg_log = logging.getLogger("nhl_betting")
    if not any(isinstance(h, _AnnotationHandler) for h in pkg_log.handlers):
        pkg_log.addHandler(_AnnotationHandler())
    try:
        cfg = load_config(args.config) if args.config else load_config()
        rep = run_dry(cfg, args.date) if args.dry_run else run_live(cfg, args.date)
        return 3 if rep.problems else 0
    except (SchemaError, OddsSchemaError) as exc:
        log.error("API RESPONSE CHANGED SHAPE: %s", exc)
        return 3
    except (ConfigError, NhlApiError, SheetsError, OSError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:  # unexpected: still show it on the run page
        log.exception("Unexpected %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
