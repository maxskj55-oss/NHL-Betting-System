"""Tonight's game lines: one row per game x market x side, priced off Pinnacle.

Run by Pull odds right after it appends to Odds_Snapshots. For each game in
Tonight that hasn't started, it takes the latest pre-start snapshot of that game
(both books from the same snapshot) and writes:

    market, side, line, fanduel_price      FanDuel's line and price
    baseline_prob, fair_price, baseline_ev Pinnacle no-vig (proportional) and EV
    auto_lean                              from baseline_ev (config: pricing)
    adjusted_ev, stake_units               sheet formulas, rewritten every run
    claude_line                            write-once, see below

Rules:
- FanDuel and Pinnacle on different puck lines or totals: no conversion is
  guessed; baseline cells stay blank and auto_lean is "pass: line mismatch".
  Pinnacle missing the market: "pass: no baseline". No FanDuel price: no row.
- claude_prob and claude_reason are never written.
- claude_line: the first time a run sees claude_prob filled with claude_line
  blank, it records the line the row showed before this run changed it (the
  line Claude priced). adjusted_ev uses claude_prob only while claude_line is
  blank or equal to the current line; otherwise it falls back to baseline_prob.
  Only Pull odds changes `line`, and it records claude_line first, so a blank
  claude_line means the line hasn't moved since Claude wrote.
- The morning job's placeholder row (market blank) becomes the game's first
  line row, unless Claude wrote on it; then it is left alone with a warning.
- Games that have started are left untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .config import PricingConfig
from .matching import events_from_snapshots, match_events
from .morning import (TONIGHT_COLUMNS, TONIGHT_FORMULAS, TONIGHT_KEY, TONIGHT_OWNED,
                      TONIGHT_PROTECTED)
from .pricing import (PASS_LINE_MISMATCH, PASS_NO_BASELINE, auto_lean, decimal_odds,
                      expected_value, fair_american, no_vig, stake_units)
from .sheets import _col_letter
from .snapshots import parse_time
from .tabs import Tab, norm, row_key
from .teams import team_code

log = logging.getLogger(__name__)

# Odds API market key -> Tonight market name.
MARKETS: dict[str, str] = {"h2h": "moneyline", "spreads": "puck_line", "totals": "total"}
MARKET_ORDER = ("moneyline", "puck_line", "total")

# Columns this module writes (claude_line only once; formulas every run).
LINE_COLUMNS: tuple[str, ...] = (
    "market", "side", "line", "fanduel_price", "baseline_prob", "fair_price",
    "baseline_ev", "auto_lean",
)
FANDUEL, PINNACLE = "fanduel", "pinnacle"
SORT_BY: tuple[str, ...] = ("start_time_utc", "game_id", "market", "side")


# ---- Formulas ----------------------------------------------------------------
def _ref(col: str) -> str:
    letter = _col_letter(TONIGHT_COLUMNS.index(col) + 1)
    return f"INDEX({letter}:{letter},r)"


def _num(x: float) -> str:
    return f"{x:g}"


def adjusted_ev_formula() -> str:
    """EV of FanDuel's price at claude_prob (if it applies to this line) else baseline_prob.

    Row-independent (INDEX on ROW()), so appends, deletes and archiving can't
    point it at the wrong row.
    """
    return (
        "=LET(r,ROW(),"
        f"price,{_ref('fanduel_price')},cp,{_ref('claude_prob')},"
        f"cl,{_ref('claude_line')},ln,{_ref('line')},bp,{_ref('baseline_prob')},"
        'p,IF(AND(cp<>"",OR(cl="",cl=ln)),cp,bp),'
        'IF(OR(p="",price=""),"",p*IF(price>0,1+price/100,1-100/price)-1))'
    )


def stake_units_formula(pricing: PricingConfig) -> str:
    """Units from adjusted_ev: first band reached (highest first), else 0; capped."""
    bands = ",".join(f"e>={_num(b.min_ev)},{_num(b.units)}" for b in pricing.stake_bands)
    return (
        f"=LET(r,ROW(),e,{_ref('adjusted_ev')},"
        f'IF(e="","",MIN({_num(pricing.max_units)},IFS({bands},TRUE,0))))'
    )


def formula_cells(pricing: PricingConfig) -> dict:
    return {"adjusted_ev": adjusted_ev_formula(), "stake_units": stake_units_formula(pricing)}


def adjusted_prob(row: dict) -> float | None:
    """Python copy of the adjusted_ev formula's probability choice (previews, tests)."""
    cp, cl, ln, bp = (row.get(c) for c in ("claude_prob", "claude_line", "line", "baseline_prob"))
    if norm(cp) != "" and (norm(cl) == "" or _same_line(cl, ln)):
        return float(cp)
    return float(bp) if norm(bp) != "" else None


# ---- Pure builders -----------------------------------------------------------
def _line(value) -> float | None:
    return None if norm(value) == "" else float(value)


def _same_line(a, b) -> bool:
    la, lb = _line(a), _line(b)
    return la == lb


def latest_pre_start(snapshots: Sequence[dict]) -> dict[str, list[dict]]:
    """{odds_event_id: rows of that game's latest snapshot taken before puck drop}."""
    best: dict[str, str] = {}
    keep: dict[str, list[dict]] = {}
    for r in snapshots:
        eid = norm(r.get("odds_event_id"))
        snap = norm(r.get("snapshot_time_utc"))
        if not eid or not snap or parse_time(snap) >= parse_time(norm(r.get("start_time_utc"))):
            continue
        if eid not in best or snap > best[eid]:
            best[eid], keep[eid] = snap, []
        if snap == best[eid]:
            keep[eid].append(r)
    return keep


@dataclass
class GameInfo:
    """A Tonight game, shaped for matching.match_events."""
    game_id: int
    home: str
    away: str
    start_utc: datetime
    cells: dict          # the morning job's game-level cells, copied to new rows


def _side_code(market: str, name: str, game: GameInfo) -> str | None:
    if market == "total":
        side = name.strip().lower()
        return side if side in ("over", "under") else None
    code = team_code(name)
    if code not in (game.home, game.away):
        return None
    return code


def _side_order(market: str, side: str, game: GameInfo) -> int:
    order = ("over", "under") if market == "total" else (game.away, game.home)
    return order.index(side) if side in order else 9


def build_lines(rows: Sequence[dict], game: GameInfo, pricing: PricingConfig) -> list[dict]:
    """Line rows for one game from one snapshot's rows (both books)."""
    books: dict[str, dict[str, dict[str, tuple[float | None, float]]]] = {}
    for r in rows:
        market = MARKETS.get(norm(r.get("market")))
        if market is None:
            continue
        books.setdefault(norm(r.get("book")), {}).setdefault(market, {})[norm(r.get("side"))] = (
            _line(r.get("line")), float(r["price_american"]))

    out: list[dict] = []
    fd, pin = books.get(FANDUEL, {}), books.get(PINNACLE, {})
    for market in MARKET_ORDER:
        fd_sides = fd.get(market, {})
        pin_sides = pin.get(market, {})
        for name, (line, price) in fd_sides.items():
            side = _side_code(market, name, game)
            if side is None:
                log.warning("Tonight: %s %s side %r doesn't match %s @ %s; skipped",
                            game.game_id, market, name, game.away, game.home)
                continue
            row = {"market": market, "side": side, "line": "" if line is None else line,
                   "fanduel_price": int(price) if price.is_integer() else price,
                   "baseline_prob": "", "fair_price": "", "baseline_ev": "", "auto_lean": ""}
            if len(pin_sides) != 2 or name not in pin_sides:
                if pin_sides:
                    log.warning("Tonight: %s %s: Pinnacle has %d sides %s; no baseline",
                                game.game_id, market, len(pin_sides), sorted(pin_sides))
                row["auto_lean"] = PASS_NO_BASELINE
            elif pin_sides[name][0] != line:
                # e.g. FanDuel Over 6.5 vs Pinnacle Over 6.0: no conversion is guessed.
                row["auto_lean"] = PASS_LINE_MISMATCH
            else:
                names = list(pin_sides)
                probs = dict(zip(names, no_vig([pin_sides[n][1] for n in names])))
                p = round(probs[name], 6)
                ev = expected_value(p, price)
                row.update(baseline_prob=p, fair_price=fair_american(p),
                           baseline_ev=round(ev, 6),
                           auto_lean=auto_lean(round(ev, 6), pricing.bet_candidate_min_ev))
            out.append(row)
    out.sort(key=lambda r: (MARKET_ORDER.index(r["market"]), _side_order(r["market"], r["side"], game)))
    return out


# ---- Applying to the Tonight tab --------------------------------------------
@dataclass
class TonightReport:
    games: int = 0
    added: int = 0
    updated: int = 0
    placeholders_converted: int = 0
    claude_lines_recorded: int = 0
    rows: list[dict] = field(default_factory=list)     # final line rows of processed games


def _games(current: Sequence[tuple[int, dict]]) -> dict[str, GameInfo]:
    games: dict[str, GameInfo] = {}
    for _, r in current:
        gid = norm(r.get("game_id"))
        if not gid or gid in games:
            continue
        try:
            start = parse_time(norm(r.get("start_time_utc")))
        except ValueError:
            log.warning("Tonight: game %s has no readable start_time_utc; skipped", gid)
            continue
        games[gid] = GameInfo(int(gid), norm(r.get("home")), norm(r.get("away")), start,
                              {c: r.get(c, "") for c in TONIGHT_OWNED})
    return games


def _check_claude_prob(gid: str, r: dict) -> None:
    cp = r.get("claude_prob")
    if norm(cp) == "":
        return
    try:
        ok = 0 < float(cp) < 1
    except (TypeError, ValueError):
        ok = False
    if not ok:
        log.warning("Tonight: game %s %s %s: claude_prob %r is not a probability between 0 and 1; "
                    "adjusted_ev will be wrong until it's fixed", gid, r.get("market"), r.get("side"), cp)


def apply(tab: Tab, snapshots: Sequence[dict], pricing: PricingConfig, now: datetime,
          match_tolerance_hours: float) -> TonightReport:
    rep = TonightReport()
    current = tab.read_rows()
    games = _games(current)
    latest = latest_pre_start(snapshots)
    matched, problems = match_events(
        events_from_snapshots([r for rows in latest.values() for r in rows]),
        list(games.values()), match_tolerance_hours)
    for p in problems:
        log.warning("Tonight: %s", p)
    lines_by_game = {str(g.game_id): build_lines(latest[eid], g, pricing)
                     for eid, g in matched.items()}

    by_key = {row_key(r, TONIGHT_KEY): (n, r) for n, r in current if norm(r.get("market"))}
    placeholders = {norm(r.get("game_id")): (n, r) for n, r in current
                    if norm(r.get("game_id")) and not norm(r.get("market"))}
    updates: list[tuple[int, dict]] = []
    new_rows: list[dict] = []
    touched: set[str] = set()

    for gid, game in sorted(games.items(), key=lambda kv: (kv[1].start_utc, kv[0])):
        if game.start_utc <= now:
            log.info("Tonight: %s @ %s has started; left as is", game.away, game.home)
            continue
        lines = lines_by_game.get(gid)
        if not lines:
            log.warning("Tonight: no pre-start FanDuel lines for %s @ %s (%s)", game.away, game.home, gid)
            continue
        rep.games += 1
        touched.add(gid)
        for line in lines:
            key = (gid, line["market"], line["side"])
            if key in by_key:
                n, r = by_key[key]
                _check_claude_prob(gid, r)
                cells = {c: v for c, v in line.items() if norm(r.get(c)) != norm(v)}
                cl = r.get("claude_line")
                if norm(r.get("claude_prob")) and not norm(cl) and norm(r.get("line")):
                    cells["claude_line"] = _line(r.get("line"))
                    cl = cells["claude_line"]
                    rep.claude_lines_recorded += 1
                if norm(r.get("claude_prob")) and norm(cl) and not _same_line(cl, line["line"]):
                    log.warning("Tonight: %s %s %s line moved %s -> %s since claude_prob; "
                                "adjusted_ev uses baseline_prob", gid, line["market"],
                                line["side"], norm(cl), norm(line["line"]))
                if cells:
                    updates.append((n, cells))
                    rep.updated += 1
            elif gid in placeholders:
                n, r = placeholders.pop(gid)
                if norm(r.get("claude_prob")) or norm(r.get("claude_reason")):
                    log.warning("Tonight: game %s's game row has Claude's notes; left as is, "
                                "line rows added beside it", gid)
                    new_rows.append(dict(game.cells, **line))
                else:
                    updates.append((n, dict(line)))
                    rep.placeholders_converted += 1
            else:
                new_rows.append(dict(game.cells, **line))

    tab.update_cells(updates)
    rep.added = tab.append_rows(new_rows, TONIGHT_KEY)
    if rep.added:
        # Keep each game's rows together (market and side names sort in display order).
        # Only when rows were added, i.e. the first pull of the day, before Claude writes.
        tab.sort_rows(SORT_BY)

    # Formulas on every line row of the games handled this run (after appends,
    # so new rows have row numbers). Rewriting them is how a config change lands.
    formulas = formula_cells(pricing)
    after = tab.read_rows()
    tab.update_cells([(n, dict(formulas)) for n, r in after
                      if norm(r.get("game_id")) in touched and norm(r.get("market"))])
    rep.rows = [dict(r, **formulas) for _, r in after
                if norm(r.get("game_id")) in touched and norm(r.get("market"))]
    leans: dict[str, int] = {}
    for r in rep.rows:
        leans[norm(r.get("auto_lean"))] = leans.get(norm(r.get("auto_lean")), 0) + 1
    log.info("SUMMARY Tonight lines: %d games priced; %d rows added, %d updated, %d game rows "
             "converted, %d claude_line recorded; auto_lean %s", rep.games, rep.added, rep.updated,
             rep.placeholders_converted, rep.claude_lines_recorded,
             ", ".join(f"{k} {v}" for k, v in sorted(leans.items())) or "none")
    return rep


__all__ = ["apply", "build_lines", "latest_pre_start", "adjusted_prob",
           "adjusted_ev_formula", "stake_units_formula", "decimal_odds", "stake_units"]

assert set(LINE_COLUMNS) <= set(TONIGHT_COLUMNS)
assert not set(LINE_COLUMNS) & (set(TONIGHT_PROTECTED) | set(TONIGHT_FORMULAS))
