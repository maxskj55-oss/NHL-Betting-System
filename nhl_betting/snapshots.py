"""Turn an /odds response into Odds_Snapshots rows.

One row per event x book x market x side. Rows for games that have already
started (start time at or before the snapshot time) are dropped before writing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from .odds_api import iso_z

log = logging.getLogger(__name__)

# Shared with the claude.ai Project's rulebook. Don't rename or reorder.
COLUMNS: tuple[str, ...] = (
    "snapshot_time_utc",
    "odds_event_id",
    "start_time_utc",
    "home_team",
    "away_team",
    "book",
    "market",
    "side",
    "line",
    "price_american",
    "price_decimal",
    "book_last_update_utc",
    "pre_start_check",
)


class SchemaError(ValueError):
    """The API response no longer has the shape this code expects."""


EVENT_KEYS = ("id", "commence_time", "home_team", "away_team", "bookmakers")
BOOK_KEYS = ("key", "markets")
MARKET_KEYS = ("key", "outcomes")
OUTCOME_KEYS = ("name", "price")


def _require(obj: Any, keys: Iterable[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise SchemaError(f"{where}: expected an object, got {type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise SchemaError(f"{where}: missing field(s) {missing}")


def parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, ValueError) as exc:
        raise SchemaError(f"Bad timestamp {value!r}") from exc


def american_to_decimal(price: float) -> float:
    if price >= 100:
        return round(1 + price / 100, 4)
    if price <= -100:
        return round(1 + 100 / abs(price), 4)
    raise SchemaError(f"Not a valid American price: {price}")


def validate_events(payload: Any) -> None:
    """Fail loudly if /events changed shape."""
    if not isinstance(payload, list):
        raise SchemaError("/events: expected a list")
    for i, ev in enumerate(payload):
        _require(ev, ("id", "commence_time", "home_team", "away_team"), f"event[{i}]")
        parse_time(ev["commence_time"])


def validate_odds(payload: Any) -> None:
    """Fail loudly if /odds changed shape."""
    if not isinstance(payload, list):
        raise SchemaError("/odds: expected a list")
    for i, ev in enumerate(payload):
        _require(ev, EVENT_KEYS, f"event[{i}]")
        parse_time(ev["commence_time"])
        for j, bk in enumerate(ev["bookmakers"]):
            _require(bk, BOOK_KEYS, f"event[{i}].bookmakers[{j}]")
            for k, mk in enumerate(bk["markets"]):
                _require(mk, MARKET_KEYS, f"event[{i}].bookmakers[{j}].markets[{k}]")
                for m, oc in enumerate(mk["outcomes"]):
                    where = f"event[{i}].bookmakers[{j}].markets[{k}].outcomes[{m}]"
                    _require(oc, OUTCOME_KEYS, where)
                    if not isinstance(oc["price"], (int, float)):
                        raise SchemaError(f"{where}: price is not a number")
                    if mk["key"] in ("spreads", "totals") and "point" not in oc:
                        raise SchemaError(f"{where}: {mk['key']} outcome has no point")


def _fmt_line(point: Any) -> str:
    if point is None:
        return ""
    return f"{float(point):g}"


def _fmt_price(price: float) -> int | float:
    return int(price) if float(price).is_integer() else price


def build_rows(
    payload: list[dict],
    snapshot_time: datetime,
    books: Iterable[str],
    markets: Iterable[str],
) -> tuple[list[dict], int]:
    """Return (rows, dropped_count). Rows are dicts keyed by COLUMNS."""
    validate_odds(payload)
    books, markets = set(books), set(markets)
    snap = iso_z(snapshot_time)
    rows: list[dict] = []
    dropped = 0

    for ev in payload:
        start = parse_time(ev["commence_time"])
        started = start <= snapshot_time
        for bk in ev["bookmakers"]:
            if bk["key"] not in books:
                continue
            for mk in bk["markets"]:
                if mk["key"] not in markets:
                    continue
                # Market-level last_update is current; bookmaker-level is deprecated.
                updated = mk.get("last_update") or bk.get("last_update")
                for oc in mk["outcomes"]:
                    if started:
                        dropped += 1
                        continue
                    rows.append({
                        "snapshot_time_utc": snap,
                        "odds_event_id": ev["id"],
                        "start_time_utc": iso_z(start),
                        "home_team": ev["home_team"],
                        "away_team": ev["away_team"],
                        "book": bk["key"],
                        "market": mk["key"],
                        "side": oc["name"],
                        "line": _fmt_line(oc.get("point")),
                        "price_american": _fmt_price(oc["price"]),
                        "price_decimal": american_to_decimal(oc["price"]),
                        "book_last_update_utc": iso_z(parse_time(updated)) if updated else "",
                        "pre_start_check": "TRUE",
                    })
        if started:
            log.warning("Dropped %s @ %s: started at %s, before snapshot %s",
                        ev["away_team"], ev["home_team"], iso_z(start), snap)
    return rows, dropped
