"""Unofficial NHL web API client (no key, no credits) and its schema check.

One endpoint is used: /v1/score/{YYYY-MM-DD}. It lists every game on that
date (ET schedule date) with start time, team codes, state, scores and how the
game ended (REG / OT / SO). The API has no official docs and can change
without notice, so validate_score() fails loudly on any missing field.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}

# Game types kept: 2 = regular season, 3 = playoffs (1 = preseason, 4 = all-star).
KEPT_GAME_TYPES = (2, 3)
# Game states: FUT/PRE scheduled, LIVE/CRIT in progress, FINAL/OFF finished.
FINAL_STATES = ("FINAL", "OFF")
KNOWN_STATES = ("FUT", "PRE", "LIVE", "CRIT", "FINAL", "OFF")
PERIOD_TYPES = ("REG", "OT", "SO")


class NhlApiError(RuntimeError):
    """The NHL API call failed or returned something unusable."""


class SchemaError(ValueError):
    """The NHL API response no longer has the shape this code expects."""


GAME_KEYS = ("id", "gameType", "gameDate", "startTimeUTC", "gameState", "gameScheduleState",
             "homeTeam", "awayTeam")
TEAM_KEYS = ("abbrev",)


def _require(obj: Any, keys: Iterable[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise SchemaError(f"{where}: expected an object, got {type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise SchemaError(f"{where}: missing field(s) {missing}")


def validate_score(payload: Any, day: str | None = None) -> None:
    """Fail loudly if /v1/score/{date} changed shape.

    Finished games must carry scores and gameOutcome.lastPeriodType, because
    settlement depends on them.
    """
    where_day = f"/v1/score/{day}" if day else "/v1/score"
    _require(payload, ("games",), where_day)
    if not isinstance(payload["games"], list):
        raise SchemaError(f"{where_day}: games is not a list")
    if day is not None and payload.get("currentDate") not in (None, day):
        raise SchemaError(f"{where_day}: currentDate is {payload.get('currentDate')!r}")
    for i, g in enumerate(payload["games"]):
        where = f"{where_day} games[{i}]"
        _require(g, GAME_KEYS, where)
        if not isinstance(g["id"], int):
            raise SchemaError(f"{where}: id is not an integer")
        if not isinstance(g["gameType"], int):
            raise SchemaError(f"{where}: gameType is not an integer")
        if g["gameState"] not in KNOWN_STATES:
            raise SchemaError(f"{where}: unknown gameState {g['gameState']!r}")
        for side in ("homeTeam", "awayTeam"):
            _require(g[side], TEAM_KEYS, f"{where}.{side}")
        if g["gameState"] in FINAL_STATES and g["gameType"] in KEPT_GAME_TYPES:
            for side in ("homeTeam", "awayTeam"):
                if not isinstance(g[side].get("score"), int):
                    raise SchemaError(f"{where}.{side}: final game has no integer score")
            _require(g, ("gameOutcome",), where)
            _require(g["gameOutcome"], ("lastPeriodType",), f"{where}.gameOutcome")
            if g["gameOutcome"]["lastPeriodType"] not in PERIOD_TYPES:
                raise SchemaError(f"{where}: unknown lastPeriodType "
                                  f"{g['gameOutcome']['lastPeriodType']!r}")


class NhlApiClient:
    def __init__(self, base_url: str, timeout_seconds: float = 20,
                 session: requests.Session | None = None,
                 max_attempts: int = 3, backoff_seconds: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._session = session or requests.Session()
        self.max_attempts = max_attempts
        self.backoff = backoff_seconds

    def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                reason = type(exc).__name__
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        raise NhlApiError(f"{path} returned a body that isn't JSON.") from None
                if resp.status_code not in RETRY_STATUSES:
                    raise NhlApiError(f"{path} returned HTTP {resp.status_code}: {resp.text[:200]}")
                reason = f"HTTP {resp.status_code}"
            if attempt == self.max_attempts:
                raise NhlApiError(f"{path} failed after {attempt} attempts ({reason})")
            log.warning("NHL API %s on %s, retrying (%d/%d)", reason, path, attempt,
                        self.max_attempts)
            time.sleep(self.backoff * attempt)
        raise NhlApiError("unreachable")  # pragma: no cover

    def get_score(self, day: date) -> dict:
        """All games on this ET date. Free, no key."""
        iso = day.isoformat()
        payload = self._get(f"/score/{iso}")
        validate_score(payload, iso)
        log.info("NHL /score/%s: %d games", iso, len(payload["games"]))
        return payload
