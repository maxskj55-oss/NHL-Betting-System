"""NHL /v1/score payloads -> Game records, plus back-to-back flags."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .nhl_api import FINAL_STATES, KEPT_GAME_TYPES, SchemaError, validate_score

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Game:
    game_id: int
    game_date: date            # NHL schedule date (ET)
    game_type: int
    start_utc: datetime
    home: str                  # NHL three-letter code
    away: str
    state: str                 # FUT, PRE, LIVE, CRIT, FINAL, OFF
    schedule_state: str        # OK, PPD (postponed), SUSP, CNCL ...
    home_score: int | None
    away_score: int | None
    last_period: str | None    # REG, OT, SO (finished games only)

    @property
    def is_final(self) -> bool:
        return self.state in FINAL_STATES

    @property
    def is_scheduled_ok(self) -> bool:
        return self.schedule_state == "OK"

    @property
    def matchup(self) -> str:
        return f"{self.away} @ {self.home}"


def _utc(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, ValueError) as exc:
        raise SchemaError(f"Bad startTimeUTC {value!r}") from exc


def parse_games(payload: dict, day: str | None = None) -> list[Game]:
    """Regular-season and playoff games in a /v1/score payload (validated first)."""
    validate_score(payload, day)
    games = []
    for g in payload["games"]:
        if g["gameType"] not in KEPT_GAME_TYPES:
            continue
        final = g["gameState"] in FINAL_STATES
        games.append(Game(
            game_id=g["id"],
            game_date=date.fromisoformat(g["gameDate"]),
            game_type=g["gameType"],
            start_utc=_utc(g["startTimeUTC"]),
            home=g["homeTeam"]["abbrev"],
            away=g["awayTeam"]["abbrev"],
            state=g["gameState"],
            schedule_state=g["gameScheduleState"],
            home_score=g["homeTeam"].get("score") if final else None,
            away_score=g["awayTeam"].get("score") if final else None,
            last_period=g["gameOutcome"]["lastPeriodType"] if final else None,
        ))
    return games


def teams_that_played(games: Iterable[Game]) -> set[str]:
    """Teams with a game (not postponed or cancelled) in this list."""
    out: set[str] = set()
    for g in games:
        if g.schedule_state in ("PPD", "CNCL"):
            continue
        out.update((g.home, g.away))
    return out


def et_date(dt: datetime) -> date:
    return dt.astimezone(ET).date()
