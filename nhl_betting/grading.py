"""Grade picks against the close: Pick_Grades (step 4, run by the morning job).

One row per graded item, appended once and never rewritten. Groups:

    auto        Tonight_Archive rows whose auto_lean is "bet candidate" or "lean"
    claude      every Picks row
    placed      Picks rows with placed = yes (at actual_price when filled)
    parlay_leg  each leg of each Parlays ticket
    parlay      each Parlays ticket

Closing snapshot: a game's latest Odds_Snapshots snapshot taken before puck drop.
CLV = decimal price taken x closing no-vig probability - 1, using Pinnacle's
close when Pinnacle's closing line for that side equals the pick's line, else
FanDuel's close at that line, else blank with the reason in `note`.

Settlement uses Results' score and OT/SO flag with the pick's own market, side
and line (not the last snapshot's line). Postponed or cancelled games are void.
An item waits (is not written) until its game is final or void.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from .odds_api import iso_z
from .picks import (AUTO, CLAUDE, LABELS, PARLAY, PARLAY_LEG, PICK_GRADES_KEY, PICK_ID_RE,
                    PICK_LABELS, PLACED, TICKET_ID_RE, is_placed, is_test, leg_ids, unique_by_id)
from .pricing import (LOSS, PUSH, VOID, WIN, FinalScore, clv, decimal_odds, no_vig,
                      parlay_result, return_per_unit, settle_pick)
from .snapshots import latest_pre_start
from .tabs import Tab, norm
from .teams import team_code

log = logging.getLogger(__name__)

MARKETS = {"h2h": "moneyline", "spreads": "puck_line", "totals": "total"}
FANDUEL, PINNACLE = "fanduel", "pinnacle"
PUSH_ASSUMPTION = ("pushed legs treated as void (assumption: FanDuel's general rules "
                   "state only the void rule)")


# ---- Closing prices ----------------------------------------------------------
@dataclass
class Close:
    """One game's closing snapshot: book -> market -> side -> (line, American price)."""
    snapshot_utc: str
    books: dict[str, dict[str, dict[str, tuple[float | None, float]]]]


def _num(value) -> float | None:
    return None if norm(value) == "" else float(value)


def _fmt(x: float | None) -> str:
    return "none" if x is None else f"{x:g}"


def _side(market: str, name: str) -> str | None:
    if market == "total":
        s = name.strip().lower()
        return s if s in ("over", "under") else None
    return team_code(name)


def closes_by_game(snapshots: Sequence[dict], game_map_rows: Iterable[dict]) -> dict[str, Close]:
    """{NHL game_id: Close} from the latest pre-start snapshot of each mapped game."""
    event_to_game = {norm(r.get("odds_event_id")): norm(r.get("game_id")) for r in game_map_rows}
    out: dict[str, Close] = {}
    for eid, rows in latest_pre_start(snapshots).items():
        gid = event_to_game.get(eid)
        if not gid:
            continue
        snap = norm(rows[0].get("snapshot_time_utc"))
        if gid in out and out[gid].snapshot_utc >= snap:
            continue
        books: dict[str, dict[str, dict[str, tuple[float | None, float]]]] = {}
        for r in rows:
            market = MARKETS.get(norm(r.get("market")))
            side = _side(market, norm(r.get("side"))) if market else None
            if side is None:
                continue
            books.setdefault(norm(r.get("book")), {}).setdefault(market, {})[side] = (
                _num(r.get("line")), float(r["price_american"]))
        out[gid] = Close(snap, books)
    return out


@dataclass
class Quote:
    snapshot_utc: str = ""
    pinnacle: tuple[float | None, float] | None = None   # (line, price) for the pick's side
    fanduel: tuple[float | None, float] | None = None
    prob: float | None = None
    book: str = ""
    note: str = ""


def closing_quote(close: Close | None, market: str, side: str, line: float | None) -> Quote:
    """Closing no-vig probability for this side at this line: Pinnacle, else FanDuel, else none."""
    if close is None:
        return Quote(note="no pre-start snapshot")
    q = Quote(snapshot_utc=close.snapshot_utc)
    usable: dict[str, float] = {}
    for book in (PINNACLE, FANDUEL):
        sides = close.books.get(book, {}).get(market, {})
        mine = sides.get(side)
        setattr(q, book, mine)
        if mine is not None and len(sides) == 2 and mine[0] == line:
            names = list(sides)
            usable[book] = dict(zip(names, no_vig([sides[n][1] for n in names])))[side]
    if PINNACLE in usable:
        q.prob, q.book = usable[PINNACLE], PINNACLE
        return q
    pin_reason = ("pinnacle has no closing price" if q.pinnacle is None
                  else f"pinnacle closing line {_fmt(q.pinnacle[0])}" if q.pinnacle[0] != line
                  else "pinnacle closing market incomplete")
    if FANDUEL in usable:
        q.prob, q.book = usable[FANDUEL], FANDUEL
        q.note = f"{pin_reason}; used fanduel no-vig close"
        return q
    q.note = (f"no close at line {_fmt(line)} (pinnacle {_fmt(q.pinnacle[0]) if q.pinnacle else 'none'}, "
              f"fanduel {_fmt(q.fanduel[0]) if q.fanduel else 'none'})")
    return q


# ---- One item ----------------------------------------------------------------
@dataclass
class Game:
    """What grading needs from a Results row."""
    game_date_et: str
    home: str
    away: str
    score: FinalScore


def games_from_results(rows: Iterable[dict]) -> dict[str, Game]:
    out = {}
    for r in rows:
        gid = norm(r.get("game_id"))
        if gid:
            out[gid] = Game(norm(r.get("game_date_et")), norm(r.get("home")), norm(r.get("away")),
                            FinalScore(int(float(r["home_score"])), int(float(r["away_score"])),
                                       norm(r.get("ot_so"))))
    return out


@dataclass
class Context:
    games: dict[str, Game]
    closes: dict[str, Close]
    void_games: dict[str, str]      # game_id -> game_date_et
    now: datetime


def _join(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def grade_item(ctx: Context, group: str, item_id: str, label: str, game_id, market: str,
               side: str, line, price, prob, stake, note: str = "") -> dict | None:
    """A Pick_Grades row, or None while the game isn't final (or void) yet."""
    gid = norm(game_id)
    market, side = norm(market).lower(), norm(side)
    side = side.lower() if market == "total" else side.upper()
    line_v, price_v = _num(line), float(price)
    row = {"group": group, "item_id": item_id, "label": norm(label), "game_id": gid,
           "market": market, "side": side, "line": "" if line_v is None else line_v,
           "price_taken": int(price_v) if price_v.is_integer() else price_v,
           "prob_at_pick": "" if norm(prob) == "" else float(prob),
           "stake_units": "" if norm(stake) == "" else float(stake),
           "graded_at_utc": iso_z(ctx.now)}
    if gid in ctx.void_games:
        row.update(game_date_et=ctx.void_games[gid], result=VOID, return_per_unit=0.0,
                   note=_join("game postponed or cancelled", note))
        return row
    game = ctx.games.get(gid)
    if game is None:
        return None
    result = settle_pick(market, side, line_v, game.score, game.home, game.away)
    q = closing_quote(ctx.closes.get(gid), market, side, line_v)
    row.update(
        game_date_et=game.game_date_et, result=result,
        return_per_unit=round(return_per_unit(result, price_v), 6),
        close_snapshot_utc=q.snapshot_utc,
        close_pinnacle_line=_cell(q.pinnacle, 0), close_pinnacle_price=_cell(q.pinnacle, 1),
        close_fanduel_line=_cell(q.fanduel, 0), close_fanduel_price=_cell(q.fanduel, 1),
        close_prob="" if q.prob is None else round(q.prob, 6), clv_book=q.book,
        clv="" if q.prob is None else round(clv(price_v, q.prob), 6),
        note=_join(q.note, note))
    return row


def _cell(pair, i):
    if pair is None or pair[i] is None:
        return ""
    v = pair[i]
    return int(v) if float(v).is_integer() and i == 1 else v


# ---- Groups ------------------------------------------------------------------
def auto_items(archive_rows: Iterable[dict]) -> list[dict]:
    out = []
    for r in archive_rows:
        lean = norm(r.get("auto_lean"))
        if lean not in LABELS or not norm(r.get("market")):
            continue
        item_id = "|".join(norm(r.get(c)) for c in ("game_date_et", "game_id", "market", "side"))
        out.append(dict(group=AUTO, item_id=item_id, label=lean, game_id=r.get("game_id"),
                        market=r.get("market"), side=r.get("side"), line=r.get("line"),
                        price=r.get("fanduel_price"), prob=r.get("baseline_prob"),
                        stake=r.get("stake_units")))
    return out


def _pick_prob(p: dict):
    return p.get("claude_prob") if norm(p.get("claude_prob")) else p.get("baseline_prob")


def pick_items(picks: dict[str, dict]) -> list[dict]:
    out = []
    for pid, p in picks.items():
        if norm(p.get("label")) not in PICK_LABELS:
            log.warning("Picks: %s label %r is not one of %s; graded anyway",
                        pid, norm(p.get("label")), ", ".join(PICK_LABELS))
        base = dict(item_id=pid, label=p.get("label"), game_id=p.get("game_id"),
                    market=p.get("market"), side=p.get("side"), line=p.get("line"),
                    prob=_pick_prob(p), stake=p.get("stake_units"),
                    note="test" if is_test(pid) else "")
        out.append(dict(base, group=CLAUDE, price=p.get("price")))
        if is_placed(p.get("placed")):
            actual = p.get("actual_price") if norm(p.get("actual_price")) else p.get("price")
            out.append(dict(base, group=PLACED, price=actual))
    return out


def grade_ticket(ctx: Context, tid: str, t: dict, picks: dict[str, dict]) -> list[dict] | None:
    """Rows for a ticket and its legs, or None while any leg's game isn't settled."""
    ids = leg_ids(t.get("leg_pick_ids"))
    missing = [i for i in ids if i not in picks]
    if not ids or missing:
        log.warning("Parlays: %s has %s; not graded", tid,
                    "no legs" if not ids else f"legs not in Picks: {', '.join(missing)}")
        return None
    test = is_test(tid) or any(is_test(i) for i in ids)
    legs = []
    for pid in ids:
        p = picks[pid]
        row = grade_item(ctx, PARLAY_LEG, f"{tid}:{pid}", p.get("label"), p.get("game_id"),
                         p.get("market"), p.get("side"), p.get("line"), p.get("price"),
                         _pick_prob(p), "", "test" if test else "")
        if row is None:
            return None
        legs.append(row)

    results = [r["result"] for r in legs]
    decimals = [decimal_odds(float(picks[pid]["price"])) for pid in ids]
    taken = t.get("actual_price") if is_placed(t.get("placed")) and norm(t.get("actual_price")) \
        else t.get("combined_price")
    full = decimal_odds(float(taken)) if norm(taken) else None
    if full is None:
        full = 1.0
        for d in decimals:
            full *= d
    result, paid = parlay_result(results, decimals, full)
    rpu = paid - 1 if result == WIN else (-1.0 if result == LOSS else 0.0)

    clvs = [r["clv"] for r in legs if r["result"] != VOID or "postponed" not in r["note"]]
    ticket_clv = ""
    if clvs and all(c != "" for c in clvs):
        prod = 1.0
        for c in clvs:
            prod *= 1 + c
        ticket_clv = round(prod - 1, 6)
    counts = ", ".join(f"{results.count(k)} {k}" for k in (WIN, LOSS, PUSH, VOID) if results.count(k))
    note = _join(f"{len(ids)} legs: {counts}",
                 PUSH_ASSUMPTION if PUSH in results else "",
                 "dropped legs: paid at remaining legs' prices" if result == WIN and paid != full else "",
                 "a leg has no CLV" if clvs and ticket_clv == "" else "",
                 "no ticket price: legs' product used" if not norm(taken) else "",
                 "test" if test else "")
    taken_v = float(taken) if norm(taken) else ""
    ticket = {"group": PARLAY, "item_id": tid, "label": "", "game_id": "", "game_date_et":
              max(r["game_date_et"] for r in legs), "market": "parlay",
              "side": ", ".join(ids), "line": "",
              "price_taken": int(taken_v) if taken_v != "" and taken_v.is_integer() else taken_v,
              "prob_at_pick": "", "stake_units": "" if norm(t.get("stake_units")) == ""
              else float(t["stake_units"]),
              "result": result, "return_per_unit": round(rpu, 6), "clv": ticket_clv,
              "note": note, "graded_at_utc": iso_z(ctx.now)}
    return legs + [ticket]


# ---- Whole run ---------------------------------------------------------------
@dataclass
class GradeReport:
    appended: dict[str, int] = field(default_factory=dict)
    waiting: int = 0
    skipped: int = 0
    rows: list[dict] = field(default_factory=list)


def build(ctx: Context, archive_rows: Iterable[dict], picks_rows: Iterable[dict],
          parlay_rows: Iterable[dict], already: set[tuple[str, str]]) -> GradeReport:
    rep = GradeReport()
    picks = unique_by_id(picks_rows, "pick_id", PICK_ID_RE, "Picks")
    tickets = unique_by_id(parlay_rows, "ticket_id", TICKET_ID_RE, "Parlays")

    for item in auto_items(archive_rows) + pick_items(picks):
        if (item["group"], item["item_id"]) in already:
            continue
        try:
            row = grade_item(ctx, item["group"], item["item_id"], item["label"], item["game_id"],
                             item["market"], item["side"], item["line"], item["price"],
                             item["prob"], item["stake"], item.get("note", ""))
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Pick_Grades: %s %s not graded: %s", item["group"], item["item_id"], exc)
            rep.skipped += 1
            continue
        if row is None:
            rep.waiting += 1
        else:
            rep.rows.append(row)

    for tid, t in tickets.items():
        if (PARLAY, tid) in already:
            continue
        try:
            rows = grade_ticket(ctx, tid, t, picks)
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Pick_Grades: parlay %s not graded: %s", tid, exc)
            rep.skipped += 1
            continue
        if rows is None:
            rep.waiting += 1
        else:
            rep.rows.extend(r for r in rows if (r["group"], r["item_id"]) not in already)
    for r in rep.rows:
        rep.appended[r["group"]] = rep.appended.get(r["group"], 0) + 1
    return rep


def apply(grades: Tab, picks: Tab, parlays: Tab, archive: Tab, results: Tab, game_map: Tab,
          snapshots: Sequence[dict], void_games: dict[str, str], now: datetime) -> GradeReport:
    """Read everything, append newly gradable rows to Pick_Grades. Picks and Parlays are read only."""
    already = {(norm(r.get("group")), norm(r.get("item_id"))) for _, r in grades.read_rows()}
    ctx = Context(games=games_from_results(r for _, r in results.read_rows()),
                  closes=closes_by_game(snapshots, (r for _, r in game_map.read_rows())),
                  void_games=void_games, now=now)
    rep = build(ctx, (r for _, r in archive.read_rows()), (r for _, r in picks.read_rows()),
                (r for _, r in parlays.read_rows()), already)
    grades.append_rows(rep.rows, PICK_GRADES_KEY)
    no_clv = sum(1 for r in rep.rows if r["group"] != PARLAY and r.get("clv", "") == "")
    log.info("SUMMARY Pick_Grades: %d rows appended (%s); %d waiting for a final; %d not gradable; "
             "%d without CLV", len(rep.rows),
             ", ".join(f"{k} {v}" for k, v in sorted(rep.appended.items())) or "none",
             rep.waiting, rep.skipped, no_clv)
    return rep
