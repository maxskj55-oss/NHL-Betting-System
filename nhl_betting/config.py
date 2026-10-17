"""Load config.yaml and read secrets from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

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
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"config.yaml is missing or has a bad value: {exc}") from exc


def get_odds_api_key() -> str:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        raise ConfigError("ODDS_API_KEY is not set (add it as a GitHub Secret).")
    return key
