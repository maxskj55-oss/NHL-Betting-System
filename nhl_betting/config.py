"""Load config.yaml and read secrets from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .pricing import StakeBand

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


class ConfigError(RuntimeError):
    """Missing or invalid configuration."""


@dataclass(frozen=True)
class OddsApiConfig:
    base_url: str
    sport: str
    markets: tuple[str, ...]
    bookmakers: tuple[str, ...]
    odds_format: str
    timeout_seconds: float

    @property
    def credits_per_pull(self) -> int:
        """Cost = markets x regions; a bookmaker list of up to 10 is one region."""
        regions = -(-len(self.bookmakers) // 10)  # ceiling division
        return len(self.markets) * regions


@dataclass(frozen=True)
class Config:
    odds_api: OddsApiConfig
    monthly_credits: int
    slate_timezone: str
    window_end_hour_local: int
    sink: str
    sheet_tab: str
    csv_path: Path
    raw_dir: Path
    events_fixture: Path
    odds_fixture: Path
    nhl_api_base_url: str
    nhl_api_timeout_seconds: float
    morning: "MorningConfig"
    pricing: "PricingConfig"
    grading: "GradingConfig"
    closing: "ClosingConfig"


@dataclass(frozen=True)
class MorningConfig:
    tonight_tab: str
    archive_tab: str
    results_tab: str
    game_map_tab: str
    results_lookback_days: int
    match_tolerance_hours: float
    csv_dir: Path
    raw_dir: Path
    fixtures_dir: Path


@dataclass(frozen=True)
class GradingConfig:
    picks_tab: str
    parlays_tab: str
    grades_tab: str


@dataclass(frozen=True)
class ClosingConfig:
    minutes_before_start: int        # wake this long before each start-time group
    handoff_after_minutes: int       # start a fresh run past this much run time (GitHub stops jobs at 6 h)
    max_handoffs: int                # chain length cap per night
    qualifying_leans: tuple[str, ...]  # Tonight auto_lean values that justify a pull


@dataclass(frozen=True)
class PricingConfig:
    bet_candidate_min_ev: float
    stake_bands: tuple[StakeBand, ...]   # highest min_ev first
    max_units: float


def _pricing(raw: dict) -> PricingConfig:
    threshold = float(raw["bet_candidate_min_ev"])
    bands = tuple(StakeBand(float(b["min_ev"]), float(b["units"])) for b in raw["stake_bands"])
    max_units = float(raw["max_units"])
    if threshold <= 0:
        raise ConfigError("pricing.bet_candidate_min_ev must be above 0")
    if not bands:
        raise ConfigError("pricing.stake_bands needs at least one band")
    if list(bands) != sorted(bands, key=lambda b: b.min_ev, reverse=True):
        raise ConfigError("pricing.stake_bands must be listed highest min_ev first")
    if min(b.min_ev for b in bands) < threshold:
        raise ConfigError("pricing.stake_bands: every min_ev must be at or above "
                          "bet_candidate_min_ev (leans and passes get 0 units)")
    if any(b.units <= 0 for b in bands) or max_units <= 0:
        raise ConfigError("pricing: units and max_units must be above 0")
    return PricingConfig(threshold, bands, max_units)


def _closing(raw: dict) -> ClosingConfig:
    c = ClosingConfig(
        minutes_before_start=int(raw["minutes_before_start"]),
        handoff_after_minutes=int(raw["handoff_after_minutes"]),
        max_handoffs=int(raw["max_handoffs"]),
        qualifying_leans=tuple(raw["qualifying_leans"]),
    )
    if not 1 <= c.minutes_before_start <= 120:
        raise ConfigError("closing.minutes_before_start must be 1-120")
    if not 30 <= c.handoff_after_minutes <= 340:
        raise ConfigError("closing.handoff_after_minutes must be 30-340 (GitHub stops jobs at 360)")
    if c.max_handoffs < 0:
        raise ConfigError("closing.max_handoffs can't be negative")
    return c


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text())
        api = raw["odds_api"]
        m = raw["morning"]
        return Config(
            odds_api=OddsApiConfig(
                base_url=api["base_url"].rstrip("/"),
                sport=api["sport"],
                markets=tuple(api["markets"]),
                bookmakers=tuple(api["bookmakers"]),
                odds_format=api["odds_format"],
                timeout_seconds=float(api["timeout_seconds"]),
            ),
            monthly_credits=int(raw["budget"]["monthly_credits"]),
            slate_timezone=raw["slate"]["timezone"],
            window_end_hour_local=int(raw["slate"]["window_end_hour_local"]),
            sink=raw["output"]["sink"],
            sheet_tab=raw["output"]["sheet_tab"],
            csv_path=_path(raw["output"]["csv_path"]),
            raw_dir=_path(raw["output"]["raw_dir"]),
            events_fixture=_path(raw["fixtures"]["events"]),
            odds_fixture=_path(raw["fixtures"]["odds"]),
            nhl_api_base_url=raw["nhl_api"]["base_url"].rstrip("/"),
            nhl_api_timeout_seconds=float(raw["nhl_api"]["timeout_seconds"]),
            morning=MorningConfig(
                tonight_tab=m["tonight_tab"],
                archive_tab=m["archive_tab"],
                results_tab=m["results_tab"],
                game_map_tab=m["game_map_tab"],
                results_lookback_days=int(m["results_lookback_days"]),
                match_tolerance_hours=float(m["match_tolerance_hours"]),
                csv_dir=_path(m["csv_dir"]),
                raw_dir=_path(m["raw_dir"]),
                fixtures_dir=_path(m["fixtures_dir"]),
            ),
            pricing=_pricing(raw["pricing"]),
            grading=GradingConfig(
                picks_tab=raw["grading"]["picks_tab"],
                parlays_tab=raw["grading"]["parlays_tab"],
                grades_tab=raw["grading"]["grades_tab"],
            ),
            closing=_closing(raw["closing"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"config.yaml is missing or has a bad value: {exc}") from exc


def get_odds_api_key() -> str:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        raise ConfigError("ODDS_API_KEY is not set (add it as a GitHub Secret).")
    return key
