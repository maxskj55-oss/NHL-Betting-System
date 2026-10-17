"""Step 4 sheet contract: Picks and Parlays (Claude, then Max) and Pick_Grades (code).

Picks and Parlays are written by the nightly picker in the claude.ai Project.
Code creates the tabs with these headers if missing and otherwise only reads
them: every column is protected, so any write from code is refused.

Don't rename or reorder these columns without telling Max: the rulebook uses them.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from .tabs import norm

log = logging.getLogger(__name__)

PICKS_COLUMNS: tuple[str, ...] = (
    "pick_id", "pick_time_utc", "game_id", "market", "side", "line", "price",
    "baseline_prob", "claude_prob", "claude_reason", "goalie_status", "label", "ev",
    "stake_units", "placed", "actual_price",
    # Added Oct 17, 2026 (end of the tab only; existing headers are extended in place):
    # an outside model's probability for the same side and line, and where it came from.
    "outside_prob", "outside_source",
)
PARLAYS_COLUMNS: tuple[str, ...] = (
    "ticket_id", "ticket_time_utc", "leg_pick_ids", "combined_price", "stake_units",
    "placed", "actual_price",
)
PICK_GRADES_COLUMNS: tuple[str, ...] = (
    "group", "item_id", "label", "game_id", "game_date_et", "market", "side", "line",
    "price_taken", "prob_at_pick", "stake_units", "result", "return_per_unit",
    "close_snapshot_utc", "close_pinnacle_line", "close_pinnacle_price",
    "close_fanduel_line", "close_fanduel_price", "close_prob", "clv_book", "clv",
    "note", "graded_at_utc",
)
PICK_GRADES_KEY: tuple[str, ...] = ("group", "item_id")

# Pick_Grades groups, graded separately.
AUTO, CLAUDE, PLACED, PARLAY_LEG, PARLAY = "auto", "claude", "placed", "parlay_leg", "parlay"
LABELS = ("bet candidate", "lean")          # Tonight auto_lean values that are graded (group auto)
# Labels a Picks row may carry. "best available" = the night's best rows picked to bet at a
# fixed small stake even though EV isn't positive; graded like any pick, kept apart by label.
PICK_LABELS = LABELS + ("best available",)
TEST_PREFIX = "TEST-"                        # graded, with "test" in the note
PICK_ID_RE = re.compile(r"^(TEST-)?\d{8}-\d{2,}$")
TICKET_ID_RE = re.compile(r"^(TEST-)?T\d{8}-\d{2,}$")


def is_placed(value) -> bool:
    return norm(value).lower() in ("yes", "y", "true")


def is_test(item_id) -> bool:
    return norm(item_id).upper().startswith(TEST_PREFIX)


def leg_ids(value) -> list[str]:
    """'20261017-01, 20261017-03' -> ['20261017-01', '20261017-03']."""
    return [p.strip() for p in norm(value).replace(";", ",").split(",") if p.strip()]


def unique_by_id(rows: Iterable[dict], id_col: str, pattern: re.Pattern, tab: str) -> dict[str, dict]:
    """Rows keyed by id. Blank ids and duplicates are skipped with a warning."""
    out: dict[str, dict] = {}
    dupes: set[str] = set()
    for r in rows:
        rid = norm(r.get(id_col))
        if not rid:
            if any(norm(v) for v in r.values()):
                log.warning("%s: a row has no %s; not graded", tab, id_col)
            continue
        if not pattern.match(rid):
            log.warning("%s: %s %r doesn't follow the agreed format; graded anyway", tab, id_col, rid)
        if rid in out:
            dupes.add(rid)
            continue
        out[rid] = r
    for rid in sorted(dupes):
        log.warning("%s: %s %s appears more than once; only the first row is graded", tab, id_col, rid)
    return out
