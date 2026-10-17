"""Betting math that must match the rulebook (CLAUDE.md, "Math").

Step 2: settlement. Step 3: no-vig probability, fair price, EV, auto leans and
stakes. Step 4: CLV, settling one pick at its own line, and parlay tickets.

Settlement rules: moneyline, puck line and totals include OT and the shootout.
The shootout winner gets +1 goal and the total gets exactly +1 goal.
Whole-number totals push.

No-vig: proportional. Each side's implied probability divided by the sum of
both sides. Prices are American; implied probabilities use the exact price, not
a rounded decimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

WIN, LOSS, PUSH, VOID = "win", "loss", "push", "void"
OVER, UNDER = "over", "under"

BET_CANDIDATE, LEAN, PASS = "bet candidate", "lean", "pass"
PASS_LINE_MISMATCH = "pass: line mismatch"
PASS_NO_BASELINE = "pass: no baseline"


# ---- Step 3: no-vig, fair price, EV, leans, stakes ---------------------------
def _check_american(price: float) -> float:
    price = float(price)
    if -100 < price < 100:
        raise ValueError(f"Not a valid American price: {price:g}")
    return price


def implied_prob(american: float) -> float:
    """Implied probability with the vig still in: -110 -> 0.5238, +150 -> 0.4."""
    a = _check_american(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def decimal_odds(american: float) -> float:
    """Exact decimal odds, computed the same way as the sheet formula."""
    a = _check_american(american)
    return 1 + a / 100 if a > 0 else 1 - 100 / a


def no_vig(prices: Sequence[float]) -> list[float]:
    """Proportional no-vig probabilities for every side of one market (same order)."""
    if len(prices) < 2:
        raise ValueError("No-vig needs every side of the market")
    implied = [implied_prob(p) for p in prices]
    total = sum(implied)
    return [p / total for p in implied]


def fair_american(prob: float) -> int:
    """American price with no vig for this probability, rounded half up: 0.5070 -> -103."""
    if not 0 < prob < 1:
        raise ValueError(f"Probability must be between 0 and 1, got {prob}")
    raw = -100 * prob / (1 - prob) if prob >= 0.5 else 100 * (1 - prob) / prob
    rounded = int(Decimal(str(abs(raw))).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return -rounded if raw < 0 else rounded


def expected_value(prob: float, american: float) -> float:
    """EV per unit staked at this price if the true win probability is `prob`."""
    return prob * decimal_odds(american) - 1


def auto_lean(ev: float, bet_candidate_min_ev: float) -> str:
    """bet candidate at or above the threshold, lean above 0, pass at 0 or below."""
    if ev >= bet_candidate_min_ev:
        return BET_CANDIDATE
    if ev > 0:
        return LEAN
    return PASS


@dataclass(frozen=True)
class StakeBand:
    min_ev: float
    units: float


def stake_units(ev: float | None, bands: Sequence[StakeBand], max_units: float) -> float:
    """Units for an EV: the first band (highest first) whose min_ev it reaches, else 0.

    Python copy of the sheet's stake_units formula, used for dry-run previews
    and tests. The sheet formula is what counts.
    """
    if ev is None:
        return 0.0
    for band in bands:
        if ev >= band.min_ev:
            return min(max_units, band.units)
    return 0.0


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


# ---- Step 4: CLV, one pick's settlement, parlays -----------------------------
MONEYLINE, PUCK_LINE, TOTAL = "moneyline", "puck_line", "total"


def clv(american_taken: float, close_prob: float) -> float:
    """Closing line value: decimal price taken x closing no-vig probability - 1."""
    if not 0 < close_prob < 1:
        raise ValueError(f"Closing probability must be between 0 and 1, got {close_prob}")
    return decimal_odds(american_taken) * close_prob - 1


def settle_pick(market: str, side: str, line: float | None, score: FinalScore,
                home: str, away: str) -> str:
    """win / loss / push for one pick at the line it was taken.

    `side` is a team code (moneyline, puck line) or over / under (total).
    """
    if market == TOTAL:
        if line is None:
            raise ValueError("A total pick needs a line")
        return settle_total(score.total, line, side)
    if side not in (home, away):
        raise ValueError(f"Side {side!r} is neither team ({away} @ {home})")
    margin = score.margin_home if side == home else -score.margin_home
    if market == MONEYLINE:
        return WIN if margin > 0 else LOSS
    if market == PUCK_LINE:
        if line is None:
            raise ValueError("A puck line pick needs a line")
        return settle_spread(margin, line)
    raise ValueError(f"Unknown market {market!r}")


def return_per_unit(result: str, american: float) -> float:
    """Profit per unit staked: decimal - 1 on a win, -1 on a loss, 0 on a push or void."""
    if result == WIN:
        return decimal_odds(american) - 1
    if result == LOSS:
        return -1.0
    if result in (PUSH, VOID):
        return 0.0
    raise ValueError(f"Unknown result {result!r}")


def parlay_result(leg_results: Sequence[str], leg_decimals: Sequence[float],
                  full_decimal: float) -> tuple[str, float | None]:
    """(result, decimal price paid) for a ticket from its legs.

    Any losing leg loses the ticket. Void legs drop out (FanDuel's rule); pushed
    legs are treated the same way (an assumption: FanDuel's general rules state
    only the void rule). With no legs dropped the ticket pays `full_decimal`
    (the ticket's own price); otherwise the product of the remaining legs'
    prices. If every leg drops out, the ticket is void.
    """
    if len(leg_results) != len(leg_decimals) or not leg_results:
        raise ValueError("Need one price per leg and at least one leg")
    if LOSS in leg_results:
        return LOSS, None
    kept = [d for r, d in zip(leg_results, leg_decimals) if r == WIN]
    if not kept:
        return VOID, None
    if len(kept) == len(leg_results):
        return WIN, full_decimal
    price = 1.0
    for d in kept:
        price *= d
    return WIN, price
