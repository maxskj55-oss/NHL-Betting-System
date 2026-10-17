"""Map Odds API events to NHL game_ids so odds can be joined to results.

A match needs the same home and away team (via teams.team_code) and start
times within a tolerance. Anything else is reported, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .games import Game
from .snapshots import parse_time
from .teams import team_code


@dataclass(frozen=True)
class OddsEvent:
    odds_event_id: str
    home_team: str
    away_team: str
    start_utc: datetime


def events_from_snapshots(rows: Iterable[dict]) -> list[OddsEvent]:
    """Distinct events in Odds_Snapshots rows (latest start time wins if it moved)."""
    latest: dict[str, tuple[str, dict]] = {}
    for r in rows:
        eid = str(r.get("odds_event_id", "")).strip()
        if not eid:
            continue
        snap = str(r.get("snapshot_time_utc", ""))
        if eid not in latest or snap > latest[eid][0]:
            latest[eid] = (snap, r)
    return [OddsEvent(eid, str(r["home_team"]), str(r["away_team"]),
                      parse_time(str(r["start_time_utc"])))
            for eid, (_, r) in sorted(latest.items())]


def match_events(events: Iterable[OddsEvent], games: Iterable[Game],
                 tolerance_hours: float) -> tuple[dict[str, Game], list[str]]:
    """Return ({odds_event_id: Game}, problems)."""
    games = list(games)
    tol = timedelta(hours=tolerance_hours)
    matched: dict[str, Game] = {}
    problems: list[str] = []
    for ev in events:
        label = f"{ev.away_team} @ {ev.home_team} ({ev.odds_event_id})"
        home, away = team_code(ev.home_team), team_code(ev.away_team)
        if home is None or away is None:
            problems.append(f"{label}: unknown team name; add it to nhl_betting/teams.py")
            continue
        near = [g for g in games if abs(g.start_utc - ev.start_utc) <= tol]
        hits = [g for g in near if g.home == home and g.away == away]
        if len(hits) == 1:
            matched[ev.odds_event_id] = hits[0]
        elif len(hits) > 1:
            problems.append(f"{label}: {len(hits)} NHL games match; not mapped")
        elif any(g.home == away and g.away == home for g in near):
            problems.append(f"{label}: NHL lists home and away the other way round; not mapped")
        else:
            problems.append(f"{label}: no NHL game within {tolerance_hours:g} h")
    return matched, problems
