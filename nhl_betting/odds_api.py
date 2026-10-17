"""The Odds API client: free events call, credit check, paid odds call.

The API key is sent as a query parameter, so request URLs are never logged or
put into exception messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from .config import OddsApiConfig

log = logging.getLogger(__name__)


class OddsApiError(RuntimeError):
    """The API call failed or returned something unusable."""


class BudgetError(RuntimeError):
    """The next paid pull would go over the credit budget."""


@dataclass(frozen=True)
class Usage:
    used: int
    remaining: int
    last: int | None  # cost of the call that returned these headers

    @classmethod
    def from_headers(cls, headers: Any) -> "Usage":
        try:
            used = int(headers["x-requests-used"])
            remaining = int(headers["x-requests-remaining"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OddsApiError(
                "Usage headers (x-requests-used / x-requests-remaining) are missing, "
                "so the credit budget can't be checked."
            ) from exc
        last_raw = headers.get("x-requests-last")
        try:
            last = int(last_raw) if last_raw is not None else None
        except ValueError:
            last = None
        return cls(used=used, remaining=remaining, last=last)


def iso_z(dt: datetime) -> str:
    """Format as the API expects: 2026-10-17T19:52:00Z (UTC, no microseconds)."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_budget(usage: Usage, cost: int, monthly_budget: int) -> None:
    """Raise BudgetError if a pull costing `cost` credits shouldn't run."""
    if usage.remaining < cost:
        raise BudgetError(
            f"Only {usage.remaining} credits remain on the account; this pull needs {cost}."
        )
    if usage.used + cost > monthly_budget:
        raise BudgetError(
            f"{usage.used} credits used this month; this pull ({cost}) would exceed "
            f"the monthly budget of {monthly_budget} in config.yaml."
        )


class OddsApiClient:
    def __init__(self, cfg: OddsApiConfig, api_key: str, session: requests.Session | None = None):
        self.cfg = cfg
        self._key = api_key
        self._session = session or requests.Session()

    def _get(self, path: str, params: dict[str, str]) -> tuple[Any, Usage]:
        url = f"{self.cfg.base_url}{path}"
        try:
            resp = self._session.get(
                url, params={"apiKey": self._key, **params}, timeout=self.cfg.timeout_seconds
            )
        except requests.RequestException as exc:
            # str(exc) can include the URL (and key); report only the type.
            raise OddsApiError(f"Request to {path} failed: {type(exc).__name__}") from None

        if resp.status_code != 200:
            snippet = resp.text[:300].replace(self._key, "***")
            raise OddsApiError(f"{path} returned HTTP {resp.status_code}: {snippet}")

        usage = Usage.from_headers(resp.headers)
        log.info(
            "%s: x-requests-used=%s x-requests-remaining=%s x-requests-last=%s",
            path, usage.used, usage.remaining, usage.last,
        )
        try:
            return resp.json(), usage
        except ValueError:
            raise OddsApiError(f"{path} returned a body that isn't JSON.") from None

    def get_events(self, start: datetime, end: datetime) -> tuple[list[dict], Usage]:
        """Free call: events in the window, plus current credit usage."""
        data, usage = self._get(
            f"/sports/{self.cfg.sport}/events",
            {"commenceTimeFrom": iso_z(start), "commenceTimeTo": iso_z(end), "dateFormat": "iso"},
        )
        if not isinstance(data, list):
            raise OddsApiError("/events did not return a list.")
        return data, usage

    def get_odds(self, start: datetime, end: datetime) -> tuple[list[dict], Usage]:
        """Paid call: markets x regions credits (3 for this config)."""
        data, usage = self._get(
            f"/sports/{self.cfg.sport}/odds",
            {
                "bookmakers": ",".join(self.cfg.bookmakers),
                "markets": ",".join(self.cfg.markets),
                "oddsFormat": self.cfg.odds_format,
                "dateFormat": "iso",
                "commenceTimeFrom": iso_z(start),
                "commenceTimeTo": iso_z(end),
            },
        )
        if not isinstance(data, list):
            raise OddsApiError("/odds did not return a list.")
        return data, usage
