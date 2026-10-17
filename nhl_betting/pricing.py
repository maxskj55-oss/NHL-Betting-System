"""Betting math that must match the rulebook (CLAUDE.md, "Math").

Step 2: settlement. Steps 3-4 add no-vig probability, EV and CLV here.

Settlement rules: moneyline, puck line and totals include OT and the shootout.
The shootout winner gets +1 goal and the total gets exactly +1 goal.
Whole-number totals push.
"""

from __future__ import annotations

from dataclasses import dataclass

WIN, LOSS, PUSH = "win", "loss", "push"
OVER, UNDER = "over", "under"


class SettlementError(ValueError):
    """A final score that can't be right (would settle bets wrongly)."""


@dataclass(frozen=True)
class FinalScore:
    """Goals for settlement: OT included, shootout winner credited +1."""
    home: int
    away: int
    ot_so: str        # REG, OT or SO

    @property
    def total(self) -> int:
        return self.home + self.away

    @property
    def margin_home(self) -> int:
        return self.home - self.away


def settlement_score(home_score: int, away_score: int, last_period: str) -> FinalScore:
    """Turn the NHL API's final score into the score bets settle on.

    The API's final score for a shootout game is expected to already credit
    the winner with one goal (e.g. 3-2 SO). If it ever reports the tie instead,
    the winner can't be known from the score alone, so this raises rather than
    guess. Any other impossible score also raises.
    """
    if last_period not in ("REG", "OT", "SO"):
        raise SettlementError(f"Unknown last period type {last_period!r}")
    if min(home_score, away_score) < 0:
        raise SettlementError(f"Negative score {home_score}-{away_score}")
    margin = abs(home_score - away_score)
    if margin == 0:
        raise SettlementError(
            f"Final {last_period} score is tied ({home_score}-{away_score}); "
            "NHL games can't end tied. If this is a shootout, the API stopped "
            "crediting the shootout goal: the settlement code must change."
        )
    if last_period in ("OT", "SO") and margin != 1:
        raise SettlementError(
            f"{last_period} game decided by {margin} goals ({home_score}-{away_score}); "
            "sudden-death OT and shootouts end by exactly one."
        )
    return FinalScore(home=home_score, away=away_score, ot_so=last_period)


def settle_spread(margin_for_side: int, line: float) -> str:
    """Puck line for one side: margin is that side's goals minus the opponent's."""
    result = margin_for_side + line
    if result > 0:
        return WIN
    if result < 0:
        return LOSS
    return PUSH


def total_outcome(total_goals: int, line: float) -> str:
    """over / under / push for a game total. Whole-number lines can push."""
    if total_goals > line:
        return OVER
    if total_goals < line:
        return UNDER
    return PUSH


def settle_total(total_goals: int, line: float, side: str) -> str:
    """win / loss / push for an Over or Under bet."""
    outcome = total_outcome(total_goals, line)
    if outcome == PUSH:
        return PUSH
    side = side.lower()
    if side not in (OVER, UNDER):
        raise ValueError(f"Total side must be Over or Under, got {side!r}")
    return WIN if outcome == side else LOSS
